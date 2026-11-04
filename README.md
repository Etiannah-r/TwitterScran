# TwitterScran

Application de recherche et de vérification de comptes X pour les entreprises.

## Installation

```powershell
pip install -r requirements.txt
```

## Lancer le programme

La commande historique reste compatible ; `cli.py` est aussi un point d'entrée :

```powershell
python main.py
# ou
python cli.py
```

Le code est organisé ainsi :

- `config/` : paramètres et scripts de lecture des pages X ;
- `src/infrastructure/` : accès MongoDB et persistance ;
- `src/services/` : comparaison des noms, recherche et traitement ;
- `data/` : progression et cache créés à l'exécution, ignorés par Git.

La progression permet de reprendre au prochain lancement : les entreprises déjà
traitées sont ignorées, y compris celles enregistrées dans l'ancien fichier
`x_social_progress.json` à la racine.
