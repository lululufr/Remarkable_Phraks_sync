# phraks_auto

Récupère automatiquement les numéros de [Phrack](https://phrack.org/),
convertit chaque article (texte d'origine, **non traduit**) en **PDF optimisé
pour la reMarkable**, et l'envoie sur la tablette via le cloud (`rmapi`).

**Un numéro = un seul PDF** (tous les articles regroupés, avec des signets PDF
pour naviguer d'un article à l'autre depuis la table des matières de la
reMarkable).

Organisation sur la tablette :

| Emplacement reMarkable        | Contenu                              |
| ----------------------------- | ------------------------------------ |
| `CYBER/PHRAKS/Phrack NN.pdf`  | le **dernier** numéro paru           |
| `CYBER/PHRAKS/OLD/Phrack *.pdf` | les numéros précédents (1 PDF chacun)|

À la sortie d'un nouveau numéro, le PDF présent dans `CYBER/PHRAKS` est déplacé
dans `CYBER/PHRAKS/OLD`, puis le nouveau numéro est déposé dans `CYBER/PHRAKS`.
Les fichiers sont nommés `Phrack NN.pdf` (NN sur 2 chiffres → tri correct).

## Pourquoi du PDF (et pas de l'EPUB)

Phrack est du texte 80 colonnes plein de code et d'art ASCII. L'EPUB reflow le
texte et casse l'alignement. Le PDF généré ici :

- page au **ratio de l'écran reMarkable** (portrait) ;
- police **Courier** (monospace, police PDF standard — aucune dépendance) ;
- **chaque page est dimensionnée individuellement** : la police est la plus
  grande possible telle que la ligne la plus large de la page tienne dans la
  largeur. Les pages de prose s'affichent donc en gros, les pages de code dense
  restent parfaitement alignées, jamais coupées ni ré-enroulées ;
- pagination « gloutonne » : chaque page est remplie au maximum selon sa propre
  police (moins de pages, pas d'espace gâché).

Réglages dans `phraks_auto.py` : `PAGE_W/PAGE_H`, `MARGIN_*`, `FONT_CAP`
(taille max), `FONT_MIN`, `LINE_FACTOR`.

## Fonctionnement

1. Lit le numéro du dernier Phrack sur `index_latest.html`.
2. Compare au dernier numéro traité (`state/last_issue.txt`).
3. Si nouveau : télécharge l'archive `phrack<N>.tar.gz`, en extrait les `.txt`.
4. Récupère les **titres propres** depuis la table d'index web du numéro
   (`/issues/<N>/1.html`), avec repli sur le bandeau `=[ … ]=` du `.txt`.
5. Génère **un seul PDF** pour le numéro (articles concaténés + signets).
6. Archive l'ancien numéro dans `OLD` puis envoie le nouveau via `rmapi`.

Aucune dépendance Python externe (stdlib uniquement).

## Prérequis : rmapi

L'upload reMarkable passe par [`rmapi`](https://github.com/ddvk/rmapi) (binaire Go).

```sh
# Récupérer la dernière release pour ton archi :
# https://github.com/ddvk/rmapi/releases
sudo install -m755 rmapi /usr/local/bin/rmapi

# Pairing initial (une seule fois) — code sur
# https://my.remarkable.com/device/desktop/connect
rmapi
```

Le pairing crée `~/.config/rmapi/rmapi.conf`. Le service systemd doit tourner
sous le **même utilisateur** que celui qui a fait le pairing (ou copier ce
fichier de conf sur le serveur).

## Utilisation manuelle

```sh
# Génère les PDF sans rien envoyer (test) :
python3 phraks_auto.py --no-upload --out ./out

# Force le retraitement du dernier numéro :
python3 phraks_auto.py --force

# Exécution normale (ce que lance systemd) :
python3 phraks_auto.py

# Backfill : publie d'anciens numéros dans OLD/<N>
python3 phraks_auto.py --backfill 1:71      # numéros 1 à 71
python3 phraks_auto.py --backfill all       # 1 jusqu'au dernier-1
python3 phraks_auto.py --backfill 60:71 --no-upload --out ./out  # test local
```

## Déploiement systemd (sur le serveur)

Service **utilisateur** (recommandé, même user que le pairing rmapi) :

```sh
mkdir -p ~/.config/systemd/user
cp systemd/phraks-auto.service ~/.config/systemd/user/
cp systemd/phraks-auto.timer   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now phraks-auto.timer

# Pour que le timer tourne sans session ouverte (serveur headless) :
sudo loginctl enable-linger $USER

# Vérifs :
systemctl --user list-timers phraks-auto.timer
systemctl --user start phraks-auto.service   # lancement immédiat
journalctl --user -u phraks-auto.service -f  # logs
```

> Adapter `WorkingDirectory` / `ExecStart` dans le `.service` si le dépôt
> n'est pas dans `~/Documents/PHRAKS_AUTO`.

## Configuration

Voir `config.example.env` → copier en `phraks.env` (lu par le service systemd).

## Notes

- **Navigation** : chaque numéro est un PDF unique avec des **signets** (un par
  article, intitulés `#NN - <titre>`) accessibles via la table des matières PDF
  de la reMarkable. Les fichiers sont nommés `Phrack NN.pdf` (tri correct).
- **Zoom** : sur les rares pages de code très dense, la police est petite par
  nécessité (80 colonnes) ; la reMarkable permet de pincer pour zoomer.
- Pas de traduction (choix assumé : texte d'origine en anglais).
