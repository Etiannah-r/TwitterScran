from pathlib import Path
from typing import Any

from PyQt6.QtCore import QStandardPaths, QTimer, QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QLabel, QMainWindow, QTextEdit, QVBoxLayout, QWidget

from configuration.config import (
    X_HOME_URL,
    X_PROFILE_URL,
    NAME_THRESHOLD,
    SEARCH_TABS,
    MAX_LOG_LINES,
    PAGE_SETTLE_DELAY_MS,
    SEARCH_RESULTS_RETRY_INTERVAL_MS,
    MAX_SEARCH_RESULTS_ATTEMPTS,
    MAX_PROFILE_ATTEMPTS,
    LOGIN_POLL_INTERVAL_MS,
    MAX_ACCOUNTS_BEFORE_PICKING_BEST,
    BEST_ACCOUNTS_KEPT,
    PAGE_LOAD_TIMEOUT_MS,
    PHASE_SEARCH,
    PHASE_PROFILE,
    LOGGED_IN_JS,
    SEARCH_RESULTS_SCRIPT,
    PROFILE_SCRIPT,
)
from Calcul.matching import name_similarity, data_similarity, flatten_fields, search_url
from connexion.database import with_retry, load_company, save_to_mongo, mark_processed


class NameTester(QMainWindow):
    def __init__(
        self,
        targets: list[dict[str, Any]],
        track_progress: bool = True,
        already_done: int = 0,
    ) -> None:
        super().__init__()
        self.targets = targets
        self.track_progress = track_progress
        self.total = len(targets)  # entreprises de ce lancement
        # Compteur affiché : il continue d'un lancement à l'autre (ex. 58/464767)
        self.already_done = already_done
        self.grand_total = already_done + self.total

        # État global
        self.running = False
        self.login_check_pending = False
        self.target_index = 0
        self.results: list[dict[str, Any]] = []  # entreprises avec au moins un lien

        # État de l'entreprise en cours
        self.doc_id: Any = None
        self.company_name = ""
        self.company_doc: dict[str, Any] = {}
        self.phase = PHASE_SEARCH
        self.tab_index = 0
        self.search_retry = 0
        self.profile_retry = 0
        self.profile_index = 0
        self.found: dict[str, dict[str, Any]] = {}
        self.candidates: list[dict[str, Any]] = []

        self.setWindowTitle(f"Test X : {self.already_done}/{self.grand_total}")
        self.resize(1240, 900)
        self._build_ui()
        self._setup_browser()
        self._start_login_polling()

    # --- Construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.status_label = QLabel("Connecte-toi à X la première fois.")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.view = QWebEngineView()

        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.view, 5)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _setup_browser(self) -> None:
        app_data_location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        profile_path = Path(app_data_location) / "twitter-browser-profile"
        profile_path.mkdir(parents=True, exist_ok=True)

        self.profile = QWebEngineProfile("TwitterBrowser", self)
        self.profile.setPersistentStoragePath(str(profile_path / "storage"))
        self.profile.setCachePath(str(profile_path / "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.loadFinished.connect(self.on_page_loaded)

        self.page_load_timer = QTimer(self)
        self.page_load_timer.setSingleShot(True)
        self.page_load_timer.setInterval(PAGE_LOAD_TIMEOUT_MS)
        self.page_load_timer.timeout.connect(self.on_load_timeout)

    def _start_login_polling(self) -> None:
        self.login_timer = QTimer(self)
        self.login_timer.setInterval(LOGIN_POLL_INTERVAL_MS)
        self.login_timer.timeout.connect(self.check_login)

        self.view.load(QUrl(X_HOME_URL))
        self.login_timer.start()
        QTimer.singleShot(1200, self.check_login)

    # --- Journal -----------------------------------------------------------

    @property
    def progress(self) -> str:
        return f"{self.already_done + self.target_index + 1}/{self.grand_total}"

    def log_message(self, message: str) -> None:
        self.log.append(message)
        self.status_label.setText(message)

    # --- Connexion à X -----------------------------------------------------

    def check_login(self) -> None:
        if self.running or self.login_check_pending:
            return
        self.login_check_pending = True
        self.page.runJavaScript(LOGGED_IN_JS, self.on_login_checked)

    def on_login_checked(self, logged_in: Any) -> None:
        self.login_check_pending = False
        if not logged_in:
            self.status_label.setText(
                "Session X non confirmée. Connecte-toi dans la fenêtre."
            )
            return
        self.login_timer.stop()
        self.start_batch()

    # --- Boucle sur toutes les entreprises ---------------------------------

    def start_batch(self) -> None:
        self.running = True
        self.target_index = 0
        self.start_company()

    def next_company(self) -> None:
        self.target_index += 1
        if self.target_index < self.total:
            QTimer.singleShot(0, self.start_company)
        else:
            self.show_final_summary()

    def show_final_summary(self) -> None:
        self.running = False
        self.log.document().setMaximumBlockCount(0)  # affichage complet, sans limite
        links_total = sum(len(item["accounts"]) for item in self.results)
        self.log_message(
            f"Résultat : {len(self.results)}/{self.total} "
            "entreprises avec au moins un lien trouvé"
        )
        self.log.append(f"Liens trouvés au total : {links_total}")
        for index, item in enumerate(self.results, start=1):
            note = "" if item["saved"] else " (non enregistré)"
            self.log.append(
                f"{index}. {item['company']} : {len(item['accounts'])} lien(s){note}"
            )
            for account in item["accounts"]:
                self.log.append(
                    f"    - {account['name']} : {account['link']} ({account['score']}%)"
                )

    def finish_company(self) -> None:
        """Mémorise l'entreprise comme traitée puis passe à la suivante."""
        if self.track_progress:
            mark_processed(self.doc_id)
        self.next_company()

    def abort(self, reason: str) -> None:
        """Arrête proprement ; au prochain lancement, on reprend à l'entreprise en cours."""
        self.page_load_timer.stop()
        self.show_final_summary()
        self.log_message(
            f"{reason} Relance le programme : la progression est sauvegardée."
        )

    def load_url(self, url: str) -> None:
        self.page_load_timer.start()
        self.view.load(QUrl(url))

    def on_load_timeout(self) -> None:
        if self.running:
            self.abort("Connexion trop longue.")

    # --- Recherche (noms) --------------------------------------------------

    def start_company(self) -> None:
        target = self.targets[self.target_index]
        self.company_name = target["companyName"]
        self.doc_id = target["_id"]
        self.phase = PHASE_SEARCH
        self.tab_index = 0
        self.found = {}
        self.candidates = []
        self.company_doc = {"companyName": self.company_name}

        self.setWindowTitle(f"Test X : {self.progress} - {self.company_name}")
        self.log_message(f"{self.progress} : {self.company_name}")

        if not self.company_name.strip():
            self.log.append("  companyName vide : entreprise ignorée.")
            self.finish_company()
            return
        try:
            self.company_doc = with_retry(load_company, self.doc_id, self.company_name)
        except Exception as error:  # noqa: BLE001
            self.abort(f"Connexion MongoDB échouée : {error}.")
            return

        self.log.append(
            f"  {len(flatten_fields(self.company_doc))} autre(s) donnée(s) à comparer."
        )
        self.load_tab()

    def load_tab(self) -> None:
        tab_name, tab_filter, query_template = SEARCH_TABS[self.tab_index]
        self.search_retry = 0
        self.log_message(f"{self.progress} : {self.company_name} - onglet {tab_name}")
        self.load_url(search_url(self.company_name, tab_filter, query_template))

    def next_tab(self) -> None:
        self.tab_index += 1
        if self.tab_index < len(SEARCH_TABS):
            self.load_tab()
        else:
            self.start_profiles()

    def on_page_loaded(self, successful: bool) -> None:
        if not self.running:
            return
        self.page_load_timer.stop()
        if not successful:
            self.abort("Connexion à X échouée.")
            return
        reader = (
            self.read_search_results
            if self.phase == PHASE_SEARCH
            else self.read_profile
        )
        QTimer.singleShot(PAGE_SETTLE_DELAY_MS, reader)

    def read_search_results(self) -> None:
        if self.running:
            self.page.runJavaScript(SEARCH_RESULTS_SCRIPT, self.process_results)

    def process_results(self, result: Any) -> None:
        if not self.running:
            return
        if not isinstance(result, dict):
            result = {}  # lecture ratée : traité comme « pas encore de résultat »
        if "/search" not in str(result.get("href") or ""):
            result = {}  # page de recherche pas encore affichée : on réessaie

        users = result.get("users")
        if not users:
            self.search_retry += 1
            if self.search_retry < MAX_SEARCH_RESULTS_ATTEMPTS:
                QTimer.singleShot(
                    SEARCH_RESULTS_RETRY_INTERVAL_MS, self.read_search_results
                )
                return
            self.log.append("  aucun compte trouvé dans cet onglet.")
            self.next_tab()
            return

        new_count = self.register_found_users(users)
        self.log.append(f"  {new_count} nouveau(x) compte(s) trouvé(s).")
        self.next_tab()

    def register_found_users(self, users: list[dict[str, Any]]) -> int:
        """Ajoute les comptes inconnus à self.found ; retourne le nombre ajouté."""
        new_count = 0
        for user in users:
            username = user.get("username") or ""
            if not username or username.casefold() in self.found:
                continue
            self.found[username.casefold()] = {
                "username": username,
                "name": user.get("name") or "",
                "tab": SEARCH_TABS[self.tab_index][0],
                "profile_text": user.get("text") or "",
            }
            new_count += 1
        return new_count

    # --- Vérification des autres données (page profil) ---------------------

    def start_profiles(self) -> None:
        """Garde seulement les comptes dont le NOM ressemble à au moins NAME_THRESHOLD %."""
        self.candidates = []
        for user in self.found.values():
            user["name_score"] = name_similarity(
                self.company_name, user["name"], user["username"]
            )
            if user["name_score"] >= NAME_THRESHOLD:
                self.candidates.append(user)

        if not self.candidates:
            self.show_results()
            return
        self.phase = PHASE_PROFILE
        self.profile_index = 0
        self.load_profile()

    def load_profile(self) -> None:
        user = self.candidates[self.profile_index]
        self.profile_retry = 0
        self.log_message(
            f"{self.progress} : {self.company_name}"
            f" - vérification @{user['username']} (nom {user['name_score']}%)"
        )
        self.load_url(X_PROFILE_URL.format(username=user["username"]))

    def read_profile(self) -> None:
        if self.running:
            self.page.runJavaScript(PROFILE_SCRIPT, self.process_profile)

    def process_profile(self, result: Any) -> None:
        if not self.running:
            return
        if not isinstance(result, dict):
            result = {}
        user = self.candidates[self.profile_index]
        if user["username"].casefold() not in str(result.get("href") or "").casefold():
            result = {}  # le profil attendu n'est pas encore affiché : on réessaie

        if not result.get("ready"):
            self.profile_retry += 1
            if self.profile_retry < MAX_PROFILE_ATTEMPTS:
                QTimer.singleShot(SEARCH_RESULTS_RETRY_INTERVAL_MS, self.read_profile)
                return
            self.log.append("  profil illisible : on garde le texte de la recherche.")
        else:
            user["profile_text"] = (
                " ".join(
                    str(result.get(key) or "")
                    for key in ("name", "bio", "items", "links")
                )
                + " "
                + user["username"]
            )

        self.profile_index += 1
        if self.profile_index < len(self.candidates):
            self.load_profile()
        else:
            self.show_results()

    # --- Résultat ----------------------------------------------------------

    def build_accounts(self) -> list[dict[str, Any]]:
        """Comptes retenus, du plus au moins ressemblant aux données de l'entreprise.

        Égalité : le nom le plus proche passe devant.
        """
        accounts: list[dict[str, Any]] = []
        for user in self.candidates:
            score, matched, total = data_similarity(
                self.company_doc, user["name_score"], user["profile_text"]
            )
            accounts.append(
                {
                    "link": X_PROFILE_URL.format(username=user["username"]),
                    "name": user["username"],
                    "score": score,  # ressemblance aux DONNÉES de l'entreprise
                    "name_score": user["name_score"],
                    "matched": matched,
                    "total": total,
                    "tab": user["tab"],
                }
            )
        accounts.sort(
            key=lambda account: (account["score"], account["name_score"]),
            reverse=True,
        )
        return accounts

    def log_accounts(self, accounts: list[dict[str, Any]]) -> None:
        if not accounts:
            self.log.append(
                f"Aucun lien ajouté : aucun nom ne ressemble à au moins {NAME_THRESHOLD}%."
            )
        for index, account in enumerate(accounts, start=1):
            self.log.append(f"{index}. link : {account['link']}")
            self.log.append(f"    name : {account['name']}")
            self.log.append(f"    pourcentage : {account['score']}%")

    def save_accounts(self, accounts: list[dict[str, Any]]) -> bool:
        """Enregistre dans MongoDB ; retourne True si le document a été mis à jour.

        Lève l'erreur de connexion MongoDB (gérée par show_results).
        """
        matched_docs = with_retry(save_to_mongo, self.doc_id, accounts)
        if matched_docs:
            self.log.append(f"MongoDB : {len(accounts)} lien(s) enregistré(s).")
            return True
        self.log.append("MongoDB : document introuvable, rien enregistré.")
        return False

    def show_results(self) -> None:
        accounts = self.build_accounts()

        self.log_message(f"{self.progress} : résultat pour « {self.company_name} » :")
        if len(accounts) > MAX_ACCOUNTS_BEFORE_PICKING_BEST:
            self.log.append(
                f"  {len(accounts)} comptes ressemblants (plus de "
                f"{MAX_ACCOUNTS_BEFORE_PICKING_BEST}) : on garde seulement "
                f"le(s) {BEST_ACCOUNTS_KEPT} plus ressemblant(s)."
            )
            accounts = accounts[:BEST_ACCOUNTS_KEPT]
        self.log_accounts(accounts)

        if accounts:
            try:
                saved = self.save_accounts(accounts)
            except Exception as error:  # noqa: BLE001
                self.abort(f"Connexion MongoDB échouée : {error}.")
                return
            self.results.append(
                {"company": self.company_name, "accounts": accounts, "saved": saved}
            )

        self.finish_company()
