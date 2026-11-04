import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode, urlparse

from config.settings import (
    IGNORED_FIELDS,
    NAME_THRESHOLD,
    SEARCH_EXCLUDED_TERMS,
    X_SEARCH_URL,
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
