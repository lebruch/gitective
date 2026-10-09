# gitective

Framework de reconnaissance GitHub, façon nxc.

## Modules en un coup d'œil

Arguments → module → informations obtenues. Tous les modules acceptent aussi
`-t/--token` et `--from-targets` (`[]` = option utilisable sans valeur).

```
gitective/
├── pyproject.toml
├── README.md
└── src/gitective/
    ├── cli.py                # point d'entrée (console_script "gitective")
    ├── core/
    │   ├── http.py               # session HTTP, pagination, rate-limit
    │   ├── console.py            # console rich partagée
    │   ├── db.py                 # stockage SQLite : targets + une table par module
    │   ├── pivots.py             # résolution des cibles -u/-r/-g/-w/--from-targets
    │   ├── lists.py               # parsing -u/-r avec support @fichier
    │   ├── targets.py            # prompt O/E/N après affichage des résultats
    │   ├── git_history.py        # parcours partagé des diffs (history, secrets)
    │   └── secrets_patterns.py   # patterns TruffleHog (secrets)
    └── modules/
        ├── emails.py     # extraction d'emails de commits          -> table "emails"
        ├── search.py     # code search GitHub                      -> table "search"
        ├── keys.py       # clés SSH/GPG publiques                  -> table "keys"
        ├── profile.py    # infos publiques de base d'un compte     -> table "profile"
        ├── gists.py      # gists publics                           -> table "gists"
        ├── history.py    # regex custom sur l'historique complet   -> table "history"
        ├── secrets.py    # patterns TruffleHog sur l'historique    -> table "secrets"
        ├── activity.py   # décalages UTC réels, heatmap heure locale -> table "activity"
        ├── repos.py      # liste des repos + stats de commits      -> table "repos"
        ├── workflows.py  # workflows GH Actions : triggers/runners -> table "workflows"
        ├── members.py    # membres publics + outside collaborators d'une org -> table "members"
        ├── aliases.py    # anciens pseudos (emails noreply des commits) -> table "aliases"
        ├── report.py     # orchestre les modules ci-dessus -> HTML
        └── db.py         # REPL SQL interactif sur la base
```

## Installation

```bash
pipx install .          # ou pipx install -e . pour dev
```

```bash
export GITHUB_TOKEN=ghp_xxx   # ou -t/--token à chaque commande
```

## Stockage : une base unique par utilisateur

`.gitective.db` (SQLite) vit dans le répertoire de données de l'utilisateur
(`~/.local/share/gitective/gitective.db` sur Linux, résolu via
[`platformdirs`](https://pypi.org/project/platformdirs/)), pas dans le
dossier courant. Deux catégories de tables :

- **Une table par module métier**, nommée exactement comme le module -
  chaque table EST l'historique complet de ce module, cumulé run après run.
- **`targets`** — un pool curé de `owner`/`repo`/`gist` (pas d'email, pas
  de clé, rien d'autre) - ce que `--from-targets` relit.

Un changement de schéma (nouvelle colonne sur un module existant) est migré
automatiquement au démarrage - pas besoin de vider ou recréer une table à la
main.

## Le flux d'un run

La commande s'exécute, sauvegarde dans la table du module, **affiche les
résultats**, puis demande :

```
Save N pivot(s) found to your targets pool? (O)ui / (E)diter / (N)on
```

- **O** (défaut) : ajoute tout. **E** : liste numérotée, retire des indices
  avant d'ajouter le reste. **N** : n'ajoute rien.

## `gitective db` : un REPL SQL, sans garde-fou

```
$ gitective db
gitective db - /home/user/.local/share/gitective/gitective.db
Type SQL directly, or !show / !flush / !export <table|all>. Ctrl-D or 'exit' to leave.

db> !show
db> !show emails
db> SELECT DISTINCT email FROM emails ORDER BY email;
db> DELETE FROM targets WHERE value = 'faux-positif';
db> !export keys
db> !flush search
db> exit
```

- `!show` seul : liste les tables + nombre de lignes.
- `!show <table>` : équivalent de `SELECT * FROM <table>`.
- `!flush <table|all>` : vide une table (confirmation demandée), ou toutes.
- `!export <table|all>` : exporte en CSV (`<table>.csv`) dans le dossier courant.
- Tout le reste est exécuté tel quel comme SQL (`DROP TABLE` compris).

## Rate limiting

Pas de gestion de "budget" de requêtes - le tool laisse tourner ce qu'il
faut. Ce qui reste géré automatiquement : un timeout réseau ou une perte de
connexion sauvegarde ce qui a été collecté et s'arrête proprement plutôt
que de planter ou de bloquer indéfiniment. L'endpoint `/search/code` a une
limite bien plus stricte (~30 req/min) que le reste de l'API, et son 403
n'a pas toujours d'en-tête `Retry-After` - `request_with_retry()` détecte
aussi un 403 dont le corps mentionne un rate limit, et `search` espace ses
pages de résultats pour éviter de le déclencher sans prévenir sur une
requête très fréquente. Un 403 qui ne correspond à aucun de ces signaux
(donc pas un rate limit) est traité comme un droit d'accès manquant sur
l'endpoint (ex: `outside_collaborators` sans droits admin sur l'org) -
levé en `SkipTarget`, géré comme un skip propre plutôt qu'un crash.

## Modules

### Users vs organisations : `-u`/`-o` et le guardrail

`repos`, `emails`, `activity`, `history` et `secrets` s'étendent tous via
un owner - qui peut être un compte user ou une org GitHub, deux choses
différentes côté API (`/users/{name}/repos` vs `/orgs/{name}/repos`).
`-u`/`--user` et `-o`/`--org` existent séparément pour rester sans
ambiguïté, mais le vrai type du compte est toujours vérifié (1 requête,
`GET /users/{name}`, fonctionne pour les deux) avant de choisir l'endpoint
: si le flag utilisé ne correspond pas à la réalité, une note s'affiche et
le bon endpoint est utilisé quand même plutôt que d'échouer. Sur
`activity`/`history`/`secrets`, seul un nom passé via `-u` (donc un compte
qui s'est avéré être un vrai user) sert de filtre auteur - une org, même
appelée par erreur via `-u`, n'en sert jamais un (les orgs ne committent
pas).

### Cibler des repos et des gists ensemble : `-u`/`-o`/`-r`/`-g`/`-w`

`history` et `secrets` peuvent scanner des repos et des gists en une seule
invocation - `-r`/`-g` fonctionnent comme des cases à cocher : donnés seuls
(sans valeur), ils restreignent quel(s) type(s) de contenu est en scope ;
donnés avec une valeur, ils ciblent quelque chose de précis.

- `-u alice` seul → tous les repos non-fork d'alice (ses propres commits) +
  tous ses gists publics.
- `-o acme-corp` seul → tous les repos non-fork de l'org (aucun filtre
  auteur ; les orgs n'ont pas de gists, ignoré silencieusement).
- `-u alice -r` (sans valeur) → pareil, mais repos seulement (pas les gists).
- `-u alice -g` (sans valeur) → pareil, gists seulement.
- `-r owner/repo` → exactement ce repo. Combiné à `-u`, `-u` devient un
  filtre auteur (peut différer de l'owner réel du repo) ; combiné à `-o`,
  jamais de filtre auteur.
- `-r nomrepo` (sans `/`) + `-u alice` (ou `-o acme-corp`) → le repo
  `nomrepo` de ce compte, qui fournit l'owner (et le filtre auteur si
  c'est bien un user).
- `-g abc123` → exactement ce gist (un ID de gist est global, `-u`/`-o`
  inutiles).
- `-w ci.yml` (nécessite `-r`) → restreint le scan de repo à ce fichier
  (un nom nu est préfixé par `.github/workflows/`).

`--from-targets` accepte un suffixe de lettres optionnel (`r`/`g`/`w`, ordre
et casse indifférents) pour restreindre quels types du pool sont repris ;
sans suffixe, tout. Les pivots `owner` s'étendent comme `-u` (type réel
détecté au moment de la relecture) ; les pivots `repo`/`gist`/`workflow`
directs sont utilisés tels quels, et les deux sources se cumulent
(contrairement à `-u`/`-o`+`-r` sur la ligne de commande, qui forment une
seule intention combinée).

Les gists n'exposent pas de diff comme les repos (chaque révision est le
contenu complet du fichier à ce moment-là) - `--scope added|removed|both`
n'a donc aucun effet sur les hits venant d'un gist, seulement sur les repos.

### Les modules

- **search** — code search GitHub. `-u`/`-r`/`-l`/`-p` sont des filtres
  injectés dans la requête (`user:`/`repo:`/`language:`/`path:`), pas une
  restriction qui énumère les repos au préalable ; `-r` gagne sur `-u` si
  les deux sont donnés. Sans `--context` : juste la liste repos/fichiers.
  `--context` seul : la ligne exacte du match. `--context N` : + N lignes
  avant/après.
- **emails** — emails author/committer des commits de repos non-fork,
  dédupliqués (owner, repo, rôle, email) plutôt que répétés à chaque commit.
  Colonnes `gh_login`/`gh_id` : le compte GitHub que l'API rattache au
  commit (gratuit, déjà dans la réponse) - relie un email à un id de compte
  stable. Les lignes d'anciens runs sont complétées au run suivant.
- **keys** — clés SSH/GPG publiques (valeur complète) ; corrélation
  d'identité via un fragment de clé réutilisable dans `search` (affiché,
  pas stocké).
- **profile** — infos publiques de base d'un compte (dont l'id numérique,
  colonne `github_id`, qui ne change jamais), avec lien GitHub et
  Twitter/X cliquables, organisations (`/users/{u}/orgs`, publiques
  seulement, aussi proposées en pivot `owner`) et comptes sociaux liés
  (`/users/{u}/social_accounts`).
- **gists** — liste les gists publics d'un ou plusieurs users (lien direct).
- **history** — regex custom sur l'historique complet des commits d'un
  repo et/ou des révisions d'un gist ; `--scope added|removed|both` (repos
  seulement). Cible via `-u`/`-r`/`-g`/`-w`/`--from-targets` (voir
  ci-dessus, section partagée avec `secrets`).
- **secrets** — même parcours que `history`, mais contre ~33 patterns
  connus (`core/secrets_patterns.py`, basés sur la liste TruffleHog) au
  lieu d'un regex custom. `--scope` par défaut sur `both` (contrairement à
  `history`) : tout l'intérêt de scanner l'historique plutôt que l'état
  actuel, c'est de retrouver un secret retiré du HEAD mais toujours présent
  dans un vieux commit ou une vieille révision de gist. Patterns
  volontairement larges, notamment `Generic API Key`/`Generic Secret` — à
  vérifier à l'œil, pas une preuve.
- **activity** — commits uniquement (l'endpoint `/events/public`, limité à
  ~90 jours, n'est plus utilisé du tout). `-u` : activité complète d'un ou
  plusieurs owners, filtrée à leurs propres commits. `-r` : activité d'un
  ou plusieurs repos précis ; combiné à `-u`, restreint le scope à ces
  repos tout en filtrant par auteur.
  **Avec un token**, les dates viennent de l'API GraphQL, qui conserve le
  **décalage UTC enregistré par git** sur la machine de l'auteur (ex.
  `+02:00`, colonne `utc_offset`) - l'API REST, elle, convertit tout en
  UTC. Affiche la répartition des décalages (nombre de commits, première /
  dernière date : une alternance `+01:00`/`+02:00` trahit l'heure d'été),
  puis heatmap heure×jour + barres par heure **en heure locale** (en UTC sans token).
  Le décalage vient de l'horloge de la machine : fait brut, peut être faux
  (VM, CI) ou modifié. **Sans token** : API REST, heures UTC seulement.
  L'estimation statistique du fuseau (top 3 + confiance, par corrélation
  avec un profil de journée type) n'est affichée qu'en repli : sans token,
  ou si tous les décalages valent `+00:00` (vrai UTC ou machine réglée en
  UTC, impossible à trancher). Un run avec token complète aussi le
  décalage des commits déjà stockés par un run REST.
- **repos** — liste les repos d'un owner avec statut fork, étoiles, forks,
  langage, et stats de commits (nombre, première/dernière date) ;
  `--no-forks` pour exclure les forks, `--no-commit-stats` pour aller plus vite.
- **workflows** — liste les workflows GitHub Actions d'un ou plusieurs
  repos (`-u`/`-o`/`-r`, même règle que `activity`) et remonte les faits
  bruts à vérifier en premier : triggers (`on:`), classification de chaque
  job en `github-hosted`/`self-hosted`/`unresolved` (labels
  `ubuntu-*`/`windows-*`/`macos-*` connus vs label `self-hosted` littéral
  ou custom vs expression non résolvable statiquement). Ni extraction de secrets (`secrets` s'en charge),
  ni détection de pattern combiné (pwn request, injection de script) - ça
  reste à l'utilisateur de corréler. Chaque workflow trouvé alimente le
  pool `targets` (kind `workflow`, format `owner/repo:chemin`), repris par
  `history`/`secrets` via `--from-targets w`.
- **members** — `-o` obligatoire (pas de détection à faire, le module
  cible toujours une org). Membres publics (`/orgs/{org}/members`) +
  outside collaborators (accès à des repos privés sans être membre,
  nécessite souvent des droits admin sur l'org - échoue proprement sinon,
  pas un crash). **Limite structurelle à connaître** : un membre qui a mis
  son appartenance en privé est invisible ici sans droits admin - jamais
  une liste exhaustive. Pivote chaque membre trouvé en `owner`, pour
  chaîner direct sur `gitective secrets --from-targets` sur toute une org.
- **report** — prend un ou plusieurs users, enchaîne `profile`/`keys`/
  `gists`/`emails`/`repos`/`activity` pour chacun, et génère un fichier
  HTML autonome par user (`<user>_report.html`, avatar inclus en base64) :
  vraie heatmap colorée et vraies barres pour l'activité, pas juste un
  résumé texte. Les emails y sont listés en adresses uniques (le détail
  par repo reste dans la table `emails`). Requêtes insensibles à la casse
  et sans borne temporelle (les modules dédupliqués comme `emails`/
  `activity` ne mettent pas à jour `found_at` sur une donnée déjà connue -
  une borne aurait pu la rendre invisible au rapport). Les prompts targets
  des sous-modules sont sautés (pivots quand même ajoutés) pour ne pas
  interrompre six fois de suite.
- **aliases** — anciens pseudos d'un ou plusieurs users (`-u` ou
  `--from-targets`), trouvés dans leurs commits. Parcourt tous les commits
  des repos non-fork du user (`--extended` : aussi `search/commits
  author:X` sur tout GitHub, découpé par année au-delà de 1000 résultats)
  et ne garde que ceux qui lui appartiennent de façon sûre (rattachés à son
  id, ou noreply portant son id). Les emails noreply figent le login du
  moment du commit : `noreply-id` (`ID+login@users.noreply.github.com`) et
  `noreply-legacy` (`login@users.noreply…`, format d'avant 2017, retenu
  seulement si le commit est rattaché à son id). Tout login différent du
  login actuel est un ancien pseudo, avec en preuve les repos où il
  apparaît. Limite : un commit rattaché affiche toujours le login
  **actuel** - sans email noreply utilisé avant le renommage, rien n'est
  trouvable. Les noms d'auteur vus dans ses commits sont affichés comme
  faits bruts, pas comme preuve. `--check` : un `GET /users/{alias}` par
  ancien pseudo, statut `free` ou `taken by …` - un ancien pseudo peut
  appartenir à quelqu'un d'autre aujourd'hui, c'est pourquoi seuls les
  logins actuels sont proposés en pivot, jamais les alias.
- **db** — REPL SQL sur la base (voir plus haut)

## Ajouter un module

Dépose un fichier dans `src/gitective/modules/` : il devient la commande
`gitective <NAME>`, sans registre à éditer. Squelette minimal :

```python
"""mymodule - what it does, in one sentence."""

from ..core.console import console
from ..core.http import API, RunInterrupted, SkipTarget, iter_pages, make_session
from ..core.pivots import resolve_users
from ..core.targets import offer_to_save_pivots

NAME = "mymodule"
HELP = "One-line description shown in 'gitective -h'"

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS mymodule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    owner TEXT, value TEXT
);
"""


def add_arguments(parser):
    """Declare the module's command-line options."""
    parser.add_argument("-u", "--user", help="Comma-separated GitHub usernames (or @file)")


def run(args, ctx):
    """Collect, save, print, then offer the pivots."""
    session = make_session(args.token)
    rows = []
    try:
        for user in resolve_users(args, ctx):              # -u ou --from-targets
            try:
                for item in iter_pages(session, f"{API}/users/{user}/..."):
                    rows.append({"owner": user, "value": item["..."]})
            except SkipTarget as exc:                      # 404/409/403 : on saute cette cible
                console.print(f"[warn][!] Skipping {user}: {exc}[/warn]")
    except RunInterrupted as exc:                          # coupure réseau : on garde l'acquis
        console.print(f"[warn][!] {exc} Saving what was collected so far.[/warn]")

    ctx.db.insert_many("mymodule", rows)                   # found_at est rempli tout seul
    console.print(rows)                                    # affichage AVANT la question
    offer_to_save_pivots(args, ctx, NAME, {"owner": sorted({r["owner"] for r in rows})})
```

Où trouver quoi dans `core/` :

- `http.py` : `make_session`, `request_with_retry` (rate limit géré),
  `iter_pages` (pagination), `fetch_json_or_none`, exceptions `SkipTarget` /
  `RunInterrupted`.
- `pivots.py` : transformer `-u/-o/-r/-g/-w/--from-targets` en cibles
  (`resolve_users`, `resolve_user_and_org_names`, `resolve_repo_scope`,
  `resolve_repo_and_gist_scope`) et le garde-fou user/org.
- `db.py` : `insert_many` (option `or_ignore` pour dédupliquer via une
  contrainte `UNIQUE`), `query`, `execute_many`. Une colonne ajoutée à
  `RESULT_SCHEMA` est créée automatiquement au lancement suivant. Un mot-clé
  SQL réservé (comme `commit`) fonctionne comme nom de colonne.
- `targets.py` : `offer_to_save_pivots` (kinds acceptés : `owner`, `repo`,
  `gist`, `workflow`).
- `git_history.py` : parcours de l'historique des repos et gists
  (`scan_all`), si ton module cherche quelque chose dans les diffs.

## Désinstallation

```bash
pipx uninstall gitective
rm -rf ~/.local/share/gitective   # si tu veux aussi supprimer la base
```
