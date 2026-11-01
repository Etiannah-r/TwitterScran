import argparse
import sys
import time

from PyQt6.QtWidgets import QApplication

from configuration.config import MONGO_URI
from connexion.database import (
    load_processed_ids,
    load_scan_cache,
    save_scan_cache,
    scan_database,
    with_retry,
)
from interface.interface import NameTester


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

    cached = load_scan_cache() if (args.name is None and not args.refresh) else None
    if cached:
        stats, targets = cached
        print(
            "Liste lue depuis le cache local (bilan du dernier scan). "
            "Utilise --refresh pour relire la base.",
            flush=True,
        )
    else:
        print(
            "Connexion à MongoDB et lecture de la collection (peut être long)...",
            flush=True,
        )
        started = time.time()
        try:
            stats, targets = with_retry(scan_database, args.name)
        except Exception as error:  # noqa: BLE001
            print(f"Erreur de connexion MongoDB ({MONGO_URI}) : {error}")
            return 1
        print(f"Lecture terminée en {time.time() - started:.0f} s.", flush=True)
        if args.name is None:
            save_scan_cache(stats, targets)

    track_progress = args.name is None
    already_done = 0
    if track_progress:
        processed = load_processed_ids()
        already_done = len(processed)
        targets = [target for target in targets if str(target["_id"]) not in processed]
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
