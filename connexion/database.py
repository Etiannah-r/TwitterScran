import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Generator

from bson import ObjectId
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import OperationFailure

from configuration.config import (
    MONGO_URI,
    MONGO_COLLECTION,
    MONGO_TIMEOUT_MS,
    MONGO_RETRIES,
    MONGO_RETRY_DELAY_S,
    SCAN_PROGRESS_EVERY,
    PROGRESS_FILE,
    SCAN_CACHE_FILE,
    MAX_LINKS_PER_COMPANY,
)

# =============================================================================
# Connexion MongoDB
# =============================================================================


@contextmanager
def mongo_client() -> Generator[MongoClient, None, None]:
    """Ouvre une connexion MongoDB et la ferme toujours à la fin."""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
    try:
        yield client
    finally:
        client.close()


@contextmanager
def mongo_collection() -> Generator[Collection, None, None]:
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


# =============================================================================
# Analyse de la base
# =============================================================================


def is_social_filled(value: Any) -> bool:
    """socialAccounts est rempli si au moins un réseau contient un lien/valeur."""
    if isinstance(value, dict):
        return any(is_social_filled(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(is_social_filled(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def format_stats(database_name: str, total: int, filled: int, absent: int) -> str:
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
            for account in accounts[:MAX_LINKS_PER_COMPANY]
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
