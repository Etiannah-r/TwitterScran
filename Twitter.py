"""Recherche des comptes X pour les entreprises dont socialAccounts est vide."""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlencode, urlparse

from bson import ObjectId
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import OperationFailure

from PyQt6.QtCore import QStandardPaths, QTimer, QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# =============================================================================
# Configuration
# =============================================================================

X_HOME_URL = "https://x.com/home"
X_SEARCH_URL = "https://x.com/search"
X_PROFILE_URL = "https://x.com/{username}"

NAME_THRESHOLD = 30  # % minimum de ressemblance du NOM pour garder le lien

# --- MongoDB -----------------------------------------------------------------
# La base est lue directement dans l'URI.
MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb://adding_social_accounts_user:45dfgd454gf26zeerf@57.129.18.234:27017/"
    "leadfeeder-social-accounts?authSource=leadfeeder-social-accounts",
)
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "adding_social_accounts")
MONGO_TIMEOUT_MS = 5000
SCAN_PROGRESS_EVERY = 20000  # au démarrage : affiche l'avancement tous les N documents lus

# Si la connexion MongoDB échoue : quelques nouvelles tentatives, puis on continue
MONGO_RETRIES = 3
MONGO_RETRY_DELAY_S = 2

# Champs du document ignorés quand on compare les données de l'entreprise
IGNORED_FIELDS = {"_id", "companyName", "socialAccounts", "numSocialAccounts"}

# --- Recherche X -------------------------------------------------------------
# Onglets de recherche X : (nom affiché, filtre f=, modèle de requête)
# {name} est remplacé par le nom de l'entreprise.
SEARCH_TABS = [
    # Onglet « Personnes » sans guillemets pour trouver les variantes.
    ("Personnes", "user", "{name}"),
    # Onglet « À la une » avec la recherche sans guillemets.
    ("À la une", "", "{name}"),
]
# Termes à exclure pour réduire le bruit si le nom est un mot courant
# (ex. ["art"] ajoute « -art » à la requête)
SEARCH_EXCLUDED_TERMS: list[str] = []

# --- Temporisations / tentatives ---------------------------------------------
PAGE_SETTLE_DELAY_MS = 1800
SEARCH_RESULTS_RETRY_INTERVAL_MS = 2000
MAX_SEARCH_RESULTS_ATTEMPTS = 12
MAX_PROFILE_ATTEMPTS = 6
LOGIN_POLL_INTERVAL_MS = 2000
MAX_LOG_LINES = 500  # le journal garde seulement les dernières lignes

# Peu de comptes ressemblants (<= MAX_ACCOUNTS_BEFORE_PICKING_BEST) : on les ajoute tous.
# Très nombreux : on garde seulement les BEST_ACCOUNTS_KEPT plus ressemblants.
MAX_ACCOUNTS_BEFORE_PICKING_BEST = 3
BEST_ACCOUNTS_KEPT = 1

# Connexion trop longue : on arrête et on demande de relancer le programme
PAGE_LOAD_TIMEOUT_MS = 30000

# Fichier de progression : entreprises déjà traitées (reprise au relancement)
PROGRESS_FILE = Path(__file__).with_name("x_social_progress.json")

# Cache local de la liste des entreprises (évite de relire toute la base à chaque lancement)
SCAN_CACHE_FILE = Path(__file__).with_name("x_social_targets_cache.json")

# Phases du traitement d'une entreprise
PHASE_SEARCH = "search"
PHASE_PROFILE = "profile"

# =============================================================================
# Scripts JavaScript injectés dans la page X
# =============================================================================

LOGGED_IN_JS = """Boolean(document.querySelector(
  '[data-testid="SideNav_AccountSwitcher_Button"], ' +
  '[data-testid="AppTabBar_Profile_Link"], ' +
  '[data-testid="AppTabBar_Home_Link"], ' +
  '[data-testid="SideNav_NewTabButton"]'
))"""

SEARCH_RESULTS_SCRIPT = (
    "(() => {\n  const loggedIn = "
    + LOGGED_IN_JS
    + """;
  const cells = Array.from(document.querySelectorAll('[data-testid="UserCell"], article[data-testid="tweet"]'));
  const users = cells.map((cell) => {
    const nameBox = cell.querySelector('[data-testid="User-Name"]') || cell;
    const spans = nameBox ? Array.from(nameBox.querySelectorAll('span')) : [];
    const name = spans.map((span) => (span.innerText || '').trim())
      .find((text) => text && !text.startsWith('@')) || '';
    const handleText = spans.map((span) => (span.innerText || '').trim())
      .map((text) => text.match(/^@([A-Za-z0-9_]{1,15})$/))
      .find(Boolean);
    const findProfileLink = (container) => Array.from(container.querySelectorAll('a[href]'))
      .find((anchor) => /^\\/[A-Za-z0-9_]{1,15}\\/?$/.test(new URL(anchor.href).pathname));
    const profileLink = findProfileLink(nameBox) || findProfileLink(cell);
    const profilePath = profileLink ? new URL(profileLink.href).pathname : '';
    const username = profilePath.match(/^\\/([A-Za-z0-9_]{1,15})\\/?$/);
    return {
      name,
      username: username ? username[1] : (handleText ? handleText[1] : ''),
      text: (cell.innerText || '').slice(0, 800),
    };
  });
  return {
    loggedIn,
    href: location.href,
    users,
    bodyText: (document.body ? document.body.innerText : '').slice(0, 4000),
  };
})()
"""
)

# Lecture de la page profil d'un compte X (bio, localisation, site web...)
PROFILE_SCRIPT = (
    "(() => {\n  const loggedIn = "
    + LOGGED_IN_JS
    + """;
  const text = (selector) => {
    const element = document.querySelector(selector);
    return element ? (element.innerText || '').trim() : '';
  };
  const links = Array.from(document.querySelectorAll(
    '[data-testid="UserProfileHeader_Items"] a[href], [data-testid="UserUrl"]'
  )).map((anchor) => (anchor.href || '') + ' ' + (anchor.innerText || '')).join(' ');
  return {
    loggedIn,
    href: location.href,
    ready: Boolean(document.querySelector('[data-testid="UserName"]')),
    name: text('[data-testid="UserName"]'),
    bio: text('[data-testid="UserDescription"]'),
    items: text('[data-testid="UserProfileHeader_Items"]'),
    links,
  };
})()
"""
)

# =============================================================================
# Ressemblance du NOM
# =============================================================================


def normalize_text(value: str) -> str:
    """Minuscules, sans espaces ni ponctuation (lettres et chiffres seulement)."""
    return "".join(char for char in value.casefold() if char.isalnum())


def significant_words(company_name: str) -> list[str]:
    """Mots du nom d'entreprise assez longs et non numériques."""
    words = [normalize_text(word) for word in re.findall(r"[^\W_]+", company_name)]
    return [word for word in words if len(word) >= 3 and not word.isdigit()]


def name_similarity(company_name: str, profile_name: str, username: str) -> int:
    """Pourcentage de ressemblance entre companyName et le nom / identifiant X."""
    company = normalize_text(company_name)
    candidates = (normalize_text(profile_name), normalize_text(username))

    # Au moins un mot du companyName (ex. « pizza ») doit apparaître dans le nom
    # ou l'identifiant X ; sinon aucune ressemblance (évite les faux 24 %).
    words = significant_words(company_name)
    if words and not any(word in candidate for word in words for candidate in candidates):
        return 0

    best = 0.0
    for candidate in candidates:
        if not candidate or not company:
            continue
        ratio = SequenceMatcher(None, company, candidate).ratio()
        # "pizza24" contenu dans "pizza24madagascar" -> au moins 85 %
        if len(company) >= 3 and company in candidate:
            ratio = max(ratio, 0.85)
        best = max(best, ratio)
    return round(100 * best)


# =============================================================================
# Ressemblance aux DONNÉES de l'entreprise
# =============================================================================


def flatten_fields(document: dict[str, Any]) -> list[str]:
    """Liste des valeurs texte du document (hors champs ignorés)."""
    values: list[str] = []

    def walk(key: str, value: Any) -> None:
        if key in IGNORED_FIELDS or value is None or isinstance(value, bool):
            return
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                walk(sub_key, sub_value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(key, item)
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
            if len(normalize_text(text)) >= 3 and text not in values:
                values.append(text)

    for field_key, field_value in document.items():
        walk(field_key, field_value)
    return values


def only_digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def value_in_profile(value: str, profile_text: str) -> bool:
    """Vrai si une donnée de l'entreprise se retrouve dans le profil X."""
    profile = normalize_text(profile_text)
    if not profile:
        return False

    # Texte exact (pour un site web : on compare seulement le domaine, sans www.)
    candidate = value
    if value.lower().startswith(("http://", "https://")):
        candidate = urlparse(value).netloc.removeprefix("www.") or value
    normalized = normalize_text(candidate)
    if normalized and normalized in profile:
        return True

    # Téléphone : comparaison sur les chiffres seulement
    digits = only_digits(value)
    if len(digits) >= 7 and digits in only_digits(profile_text):
        return True

    # Texte long (adresse, description...) : au moins 60 % des mots présents
    tokens = [normalize_text(token) for token in value.split()]
    tokens = [token for token in tokens if len(token) >= 3]
    if len(tokens) >= 2:
        hits = sum(1 for token in tokens if token in profile)
        return hits / len(tokens) >= 0.6
    return False


def data_similarity(
    company_doc: dict[str, Any], name_score: int, profile_text: str
) -> tuple[int, int, int]:
    """Pourcentage de données de l'entreprise retrouvées sur le profil X.

    Le nom compte pour 1 donnée (si son score >= NAME_THRESHOLD) ; chaque autre
    champ du document qui se retrouve dans le profil compte pour 1 donnée.
    Retourne (pourcentage, données trouvées, données totales).
    """
    fields = flatten_fields(company_doc)
    total = 1 + len(fields)
    matched = 1 if name_score >= NAME_THRESHOLD else 0
    matched += sum(1 for value in fields if value_in_profile(value, profile_text))
    return round(100 * matched / total), matched, total


def search_url(
    company_name: str, tab_filter: str, query_template: str = "{name}"
) -> str:
    query = query_template.format(name=company_name)
    if SEARCH_EXCLUDED_TERMS:
        query += " " + " ".join(f"-{term}" for term in SEARCH_EXCLUDED_TERMS)
    params = {"q": query.strip(), "src": "typed_query"}
    if tab_filter:
        params["f"] = tab_filter
    return f"{X_SEARCH_URL}?{urlencode(params)}"


# =============================================================================
# MongoDB
# =============================================================================


@contextmanager
def mongo_client() -> Iterator[MongoClient]:
    """Ouvre une connexion MongoDB et la ferme toujours à la fin."""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
    try:
        yield client
    finally:
        client.close()


@contextmanager
def mongo_collection() -> Iterator[Collection]:
    with mongo_client() as client:
        yield client.get_default_database()[MONGO_COLLECTION]


def with_retry(action: Callable[..., Any], *args: Any) -> Any:
    """Réessaie quelques fois si la connexion MongoDB échoue ; lève l'erreur ensuite."""
    for attempt in range(1, MONGO_RETRIES + 1):
        try:
            return action(*args)
        except Exception as error:  # noqa: BLE001
            if attempt == MONGO_RETRIES:
                raise
            print(
                f"  Tentative {attempt}/{MONGO_RETRIES} échouée : {error}. Nouvel essai...",
                flush=True,
            )
            time.sleep(MONGO_RETRY_DELAY_S)


def is_social_filled(value: Any) -> bool:
    """socialAccounts est rempli si au moins un réseau contient un lien/valeur."""
    if isinstance(value, dict):
        return any(is_social_filled(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(is_social_filled(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def format_stats(
    database_name: str, total: int, filled: int, absent: int
) -> str:
    """Bilan affiché avant de lancer la recherche."""
    return "\n".join(
        [
            f"Base : {database_name}",
            f"Collection utilisée : {MONGO_COLLECTION}",
            f"  Total de documents      : {total}",
            f"  socialAccounts remplis  : {filled}",
            f"  socialAccounts vides    : {total - filled}"
            f" (dont {absent} sans le champ)",
            f"  À rechercher            : {total - filled - absent}"
            " (champ socialAccounts présent et vide)",
        ]
    )


def to_target(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "_id": document["_id"],
        "companyName": str(document.get("companyName") or ""),
    }


# --- Analyse faite par le serveur MongoDB (rien n'est téléchargé en trop) -----


def server_leaf_filled(value: Any) -> dict[str, Any]:
    """Expression MongoDB : vrai si la valeur est une donnée remplie (texte, nombre...)."""
    return {
        "$switch": {
            "branches": [
                {
                    "case": {"$eq": [{"$type": value}, "string"]},
                    "then": {"$gt": [{"$strLenCP": {"$trim": {"input": value}}}, 0]},
                },
                {
                    "case": {
                        "$in": [{"$type": value}, ["array", "object", "null", "missing"]]
                    },
                    "then": False,
                },
            ],
            "default": True,
        }
    }


def server_any_value(container: Any, name: str, test: Callable[[Any], Any]) -> dict[str, Any]:
    """Expression MongoDB : vrai si une valeur de l'objet `container` passe `test`."""
    return {
        "$anyElementTrue": {
            "$map": {
                "input": {"$objectToArray": container},
                "as": name,
                "in": test(f"$${name}.v"),
            }
        }
    }


def server_if_type(value: Any, type_name: str, then: Any) -> dict[str, Any]:
    return {"$cond": [{"$eq": [{"$type": value}, type_name]}, then, False]}


def server_socials_filled() -> dict[str, Any]:
    """Expression MongoDB : socialAccounts contient SÛREMENT une donnée remplie.

    Structure rencontrée dans la base :
        {"facebook": [{"link": ..., "name": ...}], "instagram": "https://...",
         "youtube": [{...}], "youtubeStatus": "TROUVE"}
    Regarde jusqu'à 3 niveaux (réseau -> liste -> objet). Dans le doute, le
    document est gardé : Python refait ensuite le test exact avec is_social_filled.
    """

    def leaf(value: Any) -> dict[str, Any]:
        return server_leaf_filled(value)

    def element_filled(element: Any) -> dict[str, Any]:
        return {
            "$or": [
                leaf(element),
                server_if_type(
                    element, "object", server_any_value(element, "kv2", leaf)
                ),
            ]
        }

    def network_filled(value: Any) -> dict[str, Any]:
        return {
            "$or": [
                leaf(value),
                server_if_type(
                    value,
                    "array",
                    {
                        "$anyElementTrue": {
                            "$map": {
                                "input": value,
                                "as": "el",
                                "in": element_filled("$$el"),
                            }
                        }
                    },
                ),
                server_if_type(value, "object", server_any_value(value, "kv1", leaf)),
            ]
        }

    return {
        "$cond": [
            {"$eq": [{"$type": "$socialAccounts"}, "object"]},
            server_any_value("$socialAccounts", "kv0", network_filled),
            leaf("$socialAccounts"),
        ]
    }


def scan_on_server(only_name: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Bilan + entreprises à rechercher, avec le tri fait par le serveur MongoDB.

    Seuls les documents possiblement vides sont envoyés à l'ordinateur ; Python
    vérifie ensuite chacun avec le test exact (needs_search).
    """
    projection = {"companyName": 1, "socialAccounts": 1}
    with mongo_client() as client:
        database = client.get_default_database()
        collection = database[MONGO_COLLECTION]

        total = collection.count_documents({})
        absent = collection.count_documents({"socialAccounts": {"$exists": False}})
        candidates = collection.find(
            {
                "socialAccounts": {"$exists": True},
                "$expr": {"$not": [server_socials_filled()]},
            },
            projection,
        )
        empty_docs = [document for document in candidates if needs_search(document)]

        if only_name:
            target_docs = list(collection.find({"companyName": only_name}, projection))
        else:
            target_docs = empty_docs

        filled = total - absent - len(empty_docs)
        stats = format_stats(database.name, total, filled, absent)
    return stats, [to_target(document) for document in target_docs]


# --- Analyse faite sur l'ordinateur (repli : télécharge toute la collection) ---


def print_scan_progress(read: int, expected: int, started: float) -> None:
    """Affiche l'avancement de la lecture et le temps restant estimé."""
    elapsed = time.time() - started
    percent = 100 * read / expected if expected else 0
    remaining = elapsed * (expected - read) / read if 0 < read < expected else 0
    print(
        f"  {read}/{expected} lus ({percent:.0f} %) - reste environ {remaining / 60:.1f} min",
        flush=True,
    )


def scan_locally(only_name: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Lit toute la collection et fait le tri sur l'ordinateur."""
    total = filled = absent = 0
    targets: list[dict[str, Any]] = []
    projection = {"companyName": 1, "socialAccounts": 1}

    with mongo_client() as client:
        database = client.get_default_database()
        collection = database[MONGO_COLLECTION]
        expected = collection.estimated_document_count()
        print(f"Documents à lire : {expected}", flush=True)
        started = time.time()
        for document in collection.find({}, projection):
            total += 1
            if total % SCAN_PROGRESS_EVERY == 0:
                print_scan_progress(total, expected, started)
            if "socialAccounts" not in document:
                absent += 1
            if is_social_filled(document.get("socialAccounts")):
                filled += 1

            if only_name:
                is_target = document.get("companyName") == only_name
            else:
                is_target = needs_search(document)
            if is_target:
                targets.append(to_target(document))
        database_name = database.name

    return format_stats(database_name, total, filled, absent), targets


def run_with_heartbeat(action: Callable[[], Any], message: str, every_s: int = 15) -> Any:
    """Exécute `action` en affichant un signe de vie pendant l'attente."""
    done = threading.Event()

    def beat() -> None:
        started = time.time()
        while not done.wait(every_s):
            print(f"  {message} {time.time() - started:.0f} s...", flush=True)

    threading.Thread(target=beat, daemon=True).start()
    try:
        return action()
    finally:
        done.set()


def scan_database(only_name: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Bilan + entreprises à rechercher (socialAccounts PRÉSENT mais vide).

    Essaie d'abord l'analyse sur le serveur ; si MongoDB refuse la requête
    (version trop ancienne...), on lit toute la collection sur l'ordinateur.
    Si only_name est donné (mode test), seule cette entreprise est une cible.
    """
    try:
        print("Analyse faite directement sur le serveur MongoDB...", flush=True)
        return run_with_heartbeat(
            lambda: scan_on_server(only_name), "Analyse en cours sur le serveur..."
        )
    except OperationFailure as error:
        print(f"Le serveur a refusé l'analyse ({error}). Lecture complète à la place.")
        return scan_locally(only_name)


def needs_search(document: dict[str, Any]) -> bool:
    """Vrai si le champ socialAccounts est présent mais vide."""
    return "socialAccounts" in document and not is_social_filled(
        document["socialAccounts"]
    )


def load_company(doc_id: Any, company_name: str) -> dict[str, Any]:
    """Document complet de l'entreprise (sert à comparer les autres données)."""
    with mongo_collection() as collection:
        return collection.find_one({"_id": doc_id}) or {"companyName": company_name}


def save_to_mongo(doc_id: Any, accounts: list[dict[str, Any]]) -> int:
    """Enregistre les comptes X trouvés dans MongoDB (format socialAccounts.twitter)."""
    with mongo_collection() as collection:
        existing = collection.find_one({"_id": doc_id}) or {}
        social_accounts = existing.get("socialAccounts") or {}
        social_accounts["twitter"] = [
            {
                "link": account["link"],
                "name": account["name"],
                "percentage": f"{account['score']}%",
            }
            for account in accounts
        ]
        # nombre de réseaux sociaux ayant au moins un compte
        num_social_accounts = sum(1 for links in social_accounts.values() if links)
        result = collection.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "socialAccounts": social_accounts,
                    "numSocialAccounts": num_social_accounts,
                }
            },
        )
        return result.matched_count


# =============================================================================
# Progression (reprise après une coupure)
# =============================================================================


def load_processed_ids() -> set[str]:
    """Identifiants des entreprises déjà traitées lors des lancements précédents."""
    try:
        return set(json.loads(PROGRESS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def mark_processed(doc_id: Any) -> None:
    """Mémorise qu'une entreprise est traitée (avec ou sans lien trouvé)."""
    processed = load_processed_ids()
    processed.add(str(doc_id))
    PROGRESS_FILE.write_text(json.dumps(sorted(processed)), encoding="utf-8")


# =============================================================================
# Cache de la lecture de la base
# =============================================================================


def cache_key() -> str:
    """Identifie la base + collection du cache (le mot de passe n'est pas stocké)."""
    return hashlib.sha1(f"{MONGO_URI}|{MONGO_COLLECTION}".encode()).hexdigest()


def save_scan_cache(stats: str, targets: list[dict[str, Any]]) -> None:
    """Garde la liste des entreprises lues pour les prochains lancements."""
    if not all(isinstance(target["_id"], ObjectId) for target in targets):
        return  # identifiants non standard : pas de cache
    data = {
        "key": cache_key(),
        "stats": stats,
        "ids": [str(target["_id"]) for target in targets],
        "names": [target["companyName"] for target in targets],
    }
    SCAN_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_scan_cache() -> tuple[str, list[dict[str, Any]]] | None:
    """Liste mise en cache pour cette base, ou None s'il n'y en a pas."""
    try:
        data = json.loads(SCAN_CACHE_FILE.read_text(encoding="utf-8"))
        if data.get("key") != cache_key():
            return None
        targets = [
            {"_id": ObjectId(doc_id), "companyName": name}
            for doc_id, name in zip(data["ids"], data["names"])
        ]
        return data["stats"], targets
    except Exception:  # noqa: BLE001
        return None


# =============================================================================
# Interface et boucle de recherche
# =============================================================================


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


# =============================================================================
# Point d'entrée
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recherche X pour les entreprises dont socialAccounts est vide."
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Mode test : une seule entreprise (par défaut : toutes celles à rechercher).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Relit toute la base au lieu d'utiliser la liste en cache.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Avant de lancer la recherche : connexion à la base + bilan
    # Mode test (--name) : jamais de cache
    cached = load_scan_cache() if (args.name is None and not args.refresh) else None
    if cached:
        stats, targets = cached
        print(
            "Liste lue depuis le cache local (bilan du dernier scan). "
            "Utilise --refresh pour relire la base.",
            flush=True,
        )
    else:
        print("Connexion à MongoDB et lecture de la collection (peut être long)...", flush=True)
        started = time.time()
        try:
            stats, targets = with_retry(scan_database, args.name)
        except Exception as error:  # noqa: BLE001
            print(f"Erreur de connexion MongoDB ({MONGO_URI}) : {error}")
            return 1
        print(f"Lecture terminée en {time.time() - started:.0f} s.", flush=True)
        if args.name is None:
            save_scan_cache(stats, targets)

    track_progress = args.name is None  # mode test : pas de fichier de progression
    already_done = 0
    if track_progress:
        processed = load_processed_ids()
        already_done = len(processed)
        targets = [t for t in targets if str(t["_id"]) not in processed]
        print(f"Déjà traitées (reprise) : {already_done}")

    print(stats)
    print(f"Entreprises à traiter : {len(targets)}")
    if not targets:
        print("Rien à rechercher.")
        return 0

    app = QApplication(sys.argv[:1])
    app.setApplicationName("scranTwitter")
    window = NameTester(targets, track_progress, already_done)
    window.log.append(stats)
    window.show()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())