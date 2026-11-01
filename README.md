# TwitterScran

Application de recherche et de vérification de comptes X pour les entreprises.

## Lancer le programme

```powershell
python main.py
```

Le point d’entrée `main.py` orchestre les modules métier :

- `configuration/` : paramètres et scripts de lecture des pages X ;
- `Calcul/` : comparaison des noms et des données ;
- `connexion/` : lecture, écriture et suivi dans MongoDB ;
- `interface/` : interface de traitement.

`Twitter.py` reste disponible comme ancien point d’entrée.
