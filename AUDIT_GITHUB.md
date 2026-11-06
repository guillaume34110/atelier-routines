# Audit du projet et proposition GitHub

31 octobre 2026. Audit architectural du dépôt, approfondi sur Git, GitHub et les comptes. Charge visée : une personne, deux comptes et plusieurs chats pouvant travailler sur des dépôts différents.

Atelier Routines transforme des opérations répétitives en commandes bornées, communes à la CLI et au MCP, avec des preuves et un suivi durable. Le socle convient au problème GitHub : il faut lui ajouter la résolution du compte et rendre exécutables les procédures de PR déjà présentes.

**Le blocage observé est concret : les deux accès testés utilisent `guillaume-rebaze`, avec `push=false` sur `guillaume34110/atelier-routines`.** Aucun mécanisme existant ne choisit un autre compte pour ce dépôt. Une gestion déterministe de l'identité, des droits et du cycle PR doit prendre en charge cette répétition.

## Vue du projet

| Élément | État constaté | Conséquence |
| --- | --- | --- |
| Interface | Deux outils MCP : `routines_find`, `routines_run` ; exécuteur partagé avec la CLI | Conserver cette interface |
| Catalogue | 110 procédures, 3 propositions exclues, 39 schémas exécutables | Distinguer procédure décrite et opération réellement exécutée |
| Git | 17 entrées ; 7 exécutables, 10 guidées | Les inspections locales fonctionnent ; le cycle PR reste à réaliser |
| Exécutions | `request_id`, `execution_id`, preuves, inspection et reprise | Réutiliser ce suivi pour les opérations GitHub |
| Recettes | Composition limitée aux schémas autorisés | Garder les écritures GitHub hors des recettes génériques d'inspection |
| Dépendances | SDK MCP et bibliothèque standard Python | Réutiliser `gh`, sans nouvelle dépendance |
| Validation | 13 fichiers de tests au premier niveau de `scripts/` ; CI `verify` et `security` | Étendre les scénarios existants et conserver la suite complète |
| Mesure | Collecte locale dans `scripts/usage.py`, benchmark et preuves existants | Mesurer les économies réelles avec ces outils |

Les inspections Git exécutables sont `git.status`, `git.diff`, `git.staged`, `git.log`, `git.blame`, `git.branches` et `git.remote`.

Points solides à conserver : paramètres fermés, commandes sans shell fourni par l'appelant, sorties bornées, protection des chemins, filtrage des variables d'environnement, preuves privées et états non réussis explicites. La relecture du code et la revue des PR restent des tâches de raisonnement ; les routines doivent fournir le contexte et exécuter les étapes mécaniques.

## Constats à corriger

### Indispensable

1. **Aucun lien entre dépôt, compte et droits.**
   - **Rôle :** identifier le dépôt avant une action GitHub. [Le schéma actuel](/Users/guillaumegaillard/Documents/ChatGPT/Routines/scripts/executor.py:77) ne couvre que les références Git ; [le catalogue](/Users/guillaumegaillard/Documents/ChatGPT/Routines/references/catalogue.json:629) demande ensuite de choisir la bonne connexion.
   - **Problème :** `gh auth status` expose un compte actif, `guillaume-rebaze`. Le connecteur renvoie le même login. Les deux accès donnent `pull=true`, `push=false`, `admin=false` pour ce dépôt. L'écriture ne peut donc pas aboutir avec ces accès.
   - **Correction :** une routine de contexte résout le dépôt, le compte configuré, l'identité réellement authentifiée et les droits nécessaires avant l'action. Elle renvoie un blocage précis si le compte manque.
   - **Sans correction :** chaque chat doit redécouvrir les comptes et les permissions, puis échouer sur les mêmes prérequis.

2. **`git.remote` perd la destination de poussée lorsqu'elle diffère de celle de lecture.**
   - **Rôle :** identifier les dépôts distants. [Le parseur](/Users/guillaumegaillard/Documents/ChatGPT/Routines/scripts/executor.py:513) parcourt `git remote -v` et ignore toute nouvelle ligne portant un nom de remote déjà vu.
   - **Problème :** avec `origin` en lecture sur un dépôt et un `pushurl` sur un fork ou un autre compte, seule la première URL est conservée. Le résultat ne distingue pas les deux destinations.
   - **Correction :** conserver séparément toutes les destinations de lecture et de poussée, normaliser le dépôt et refuser une écriture ambiguë. Étendre [le test existant](/Users/guillaumegaillard/Documents/ChatGPT/Routines/scripts/test_expanded.py:87) au cas de destinations différentes.
   - **Sans correction :** un futur automatisme peut choisir le mauvais dépôt ou le mauvais compte pour pousser ou créer une PR.

### À traiter dans ce chantier

3. **Les routines de PR sont des instructions à réinterpréter.**
   - **Rôle :** création, contrôles, protections et fusion. Les entrées existent déjà dans [le catalogue](/Users/guillaumegaillard/Documents/ChatGPT/Routines/references/catalogue.json:558).
   - **Problème :** `git.pr-create`, `git.pr-checks`, `git.protection`, `git.merge` et `git.integration` n'ont pas de schéma exécutable. L'appel réel à `git.protection` a retourné `guided`, avec une phrase de procédure.
   - **Correction :** implémenter ces IDs derrière un adaptateur GitHub commun, avec paramètres fermés, contexte vérifié et reprises connues. Conserver la revue humaine ou par modèle pour le contenu du code.
   - **Sans correction :** le modèle reconstruit les commandes, les appels API, les contrôles et leur enchaînement à chaque demande.

4. **La réduction des réponses rend certaines inspections difficiles à exploiter.**
   - **Rôle :** limiter le volume retourné. [La fonction `compact`](/Users/guillaumegaillard/Documents/ChatGPT/Routines/scripts/executor.py:927) remplace les détails par une chaîne JSON coupée à 500 caractères.
   - **Problème :** la recherche `github` a renvoyé 16 occurrences mais un aperçu coupé au milieu d'un objet. Le JSON extérieur reste valide ; l'aperçu intérieur ne constitue plus une liste exploitable.
   - **Correction :** renvoyer quelques résultats structurés, les états et le résumé nécessaires à la décision suivante ; signaler la suite disponible dans la preuve, accessible avec `executions.inspect`.
   - **Sans correction :** le client doit relire une preuve ou refaire une extraction pour obtenir des informations pourtant déjà calculées.

## Solution proposée

### Un contexte de compte explicite par dépôt

Ajouter `github.context(project)`. Il produit un petit résultat : dépôt canonique, destinations Git, compte attendu, compte authentifié, branche/base, droits et éventuel blocage.

L'association est enregistrée une fois dans la configuration Git locale, par exemple `atelier.githubAccount=guillaume34110` pour ce dépôt **si ce compte est retenu**. Le propriétaire du dépôt ne détermine jamais automatiquement le compte : un dépôt d'organisation peut être utilisé par plusieurs personnes.

L'identité de commit (`user.name`, `user.email`), l'identité API et l'authentification du transport Git sont vérifiées séparément. Les worktrees réutilisent la configuration du dépôt ; aucun compte n'est basculé globalement pendant une opération.

Pour les API, l'adaptateur peut récupérer en interne le jeton du compte nommé avec [`gh auth token --user`](https://cli.github.com/manual/gh_auth_token), puis l'appliquer au seul sous-processus concerné avec `GH_TOKEN`. Cette variable prime sur les identifiants stockés : l'environnement doit donc être maîtrisé et `/user` doit confirmer le login attendu. Le jeton reste en mémoire, hors arguments, réponses et preuves. [Documentation de l'environnement `gh`](https://cli.github.com/manual/gh_help_environment).

Pour Git, réutiliser le mécanisme existant de credentials HTTPS ou les alias SSH, en sélectionnant explicitement l'identité du transport. Un jeton API ne garantit pas à lui seul que `git push` utilisera le bon compte. [Gestion de plusieurs comptes GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-multiple-accounts).

Un compte absent ou expiré donne `unavailable` avec l'action de connexion nécessaire ; une association ambiguë donne `decision_required`. Aucun essai successif de comptes, aucune nouvelle demande de connexion à chaque PR et aucune modification automatique de l'identité Git.

### Un cycle PR pris en charge par les routines existantes

| Routine | Comportement proposé |
| --- | --- |
| `github.context` — nouvelle | Résoudre l'identité, les destinations et les capacités ; produire le diagnostic utile |
| `github.prs` — nouvelle | Lister les PR des dépôts configurés pour les deux comptes, avec limites et pagination |
| `git.pr-create` | Vérifier la validation de la révision finale et la branche autorisée, pousser vers la destination vérifiée, retrouver ou créer la PR exacte |
| `git.pr-checks` | Lire les contrôles requis sur le SHA courant, attendre de façon bornée côté routine et retourner le premier échec utile |
| `git.protection` | Lire protections classiques et règles effectives ; distinguer refus d'accès, règles absentes et conformité vérifiée |
| `git.merge` | Relire SHA, base, droits, protections et contrôles juste avant fusion ; transmettre le SHA attendu au serveur |
| `git.integration` | Enchaîner les opérations autorisées en conservant une référence durable et des points de reprise |

Une même demande interrompue conserve son `request_id` et son `execution_id`. Après une réponse réseau incertaine, la reprise recherche l'effet de l'opération connue avant toute nouvelle écriture. Un reçu historique ne remplace jamais la vérification actuelle du compte, des droits, du SHA ou des contrôles.

L'absence, l'annulation ou l'échec d'un contrôle obligatoire bloque la fusion. Un nouveau commit invalide la validation précédente. La branche doit être à jour avec sa cible ; aucun mode administrateur ni contournement n'est ajouté. Les règles de branche, d'attribution et de référence Shortcut du projet restent applicables.

### Un changement contenu

Créer un seul adaptateur, `scripts/github.py`, pour les appels fixes à `gh`, la sélection du compte et la classification des erreurs. Le brancher dans l'exécuteur, le catalogue et le suivi existants. Garder les deux outils MCP.

Les modifications doivent aussi couvrir :

- le filtrage d'environnement de `capture`, qui retire actuellement `GH_TOKEN` : injection privée et limitée à l'adaptateur, sans paramètre d'environnement arbitraire ;
- [l'autorisation des recettes](/Users/guillaumegaillard/Documents/ChatGPT/Routines/scripts/workflows.py:112), pour empêcher qu'un nouveau schéma d'écriture devienne automatiquement composable ;
- [les annotations MCP](/Users/guillaumegaillard/Documents/ChatGPT/Routines/scripts/mcp_server.py:29), actuellement fixées à `openWorldHint=False` et `destructiveHint=False`, pour décrire honnêtement les nouvelles capacités réseau et d'écriture ;
- les tests de transport CLI/MCP, les déclencheurs du catalogue, la documentation et la vérification de l'installation.

Ordre utile : contexte des comptes et destinations Git, puis lecture des PR/contrôles/protections, puis création et fusion avec reprise. Le chantier ne demande ni nouveau modèle, ni nouvelle base de credentials, ni ordonnanceur supplémentaire.

## Validation attendue avant livraison du code

Les scénarios décisifs sont : deux comptes avec le mauvais compte global actif ; compte absent, expiré ou sans droits ; jeton ambiant d'un autre compte ; destinations de lecture/poussée différentes ; sessions concurrentes sans changement global ; reprise après création distante incertaine sans doublon ; nouveau SHA ou base déplacée ; contrôle manquant, annulé ou rouge ; absence de secrets dans les reçus.

Les tests automatisés utilisent des identifiants synthétiques et un service HTTP de recette avec assertions réelles. La sélection du compte et le transport Git nécessitent aussi une vérification native avec les accès concernés. Un cycle réel de création/fusion se valide ensuite dans un dépôt de recette autorisé.

Sur la révision finale, exécuter la régression et les contrôles de sécurité complets des workflows : calcul portable sur Atelier VPS, intégrations macOS en local, puis `verify` et `security` verts sur la dernière révision de la PR avant toute intégration.

Mesurer trois parcours avec la collecte existante : diagnostic d'un compte incorrect, création d'une PR, attente des contrôles puis fusion. Comparer appels outil, octets retournés, répétitions et tokens réellement disponibles. Aucun pourcentage d'économie n'est établi par cet audit.

## Preuves et limites de cet audit

- Le checkout présentait déjà 16 entrées de statut : modifications locales et fichiers non suivis. Aucune n'a été modifiée par cet audit ; seul ce rapport a été ajouté.
- Catalogue vérifié : 113 entrées et 39 schémas, dont 7 exécutables sur les 17 entrées Git. Preuve `files.json`, exécution `efeed7713308461b96e20c6ca954b975`.
- Remote observé : HTTPS, `github.com`, `guillaume34110/atelier-routines.git`. Exécution `git.remote` : `efb853859203494ab310f0ca41b68bc5`.
- Recherche observée : 16 occurrences de `github`, réponse tronquée. Exécution `project.search` : `0d0a35ed22d841bbb55e78a0794e62d3`.
- Accès en lecture vérifiés avec le connecteur et `gh` : login `guillaume-rebaze`, dépôt public, branche par défaut `main`, permissions `{pull: true, push: false, admin: false}`. Ces observations décrivent le contexte du 31 octobre 2026.
- La protection classique de `main` a renvoyé HTTP 404 ; les règles effectives interrogées ont renvoyé `[]`. Avec les droits disponibles, cela ne valide pas la conformité des protections et ne permet pas d'en attribuer sûrement la cause.
- Audit statique et requêtes distantes en lecture uniquement. Aucun changement d'authentification, push, création de PR ou fusion. La suite de tests n'a pas été exécutée ; aucun résultat de CI n'est déclaré vert. Les gardes, le VPS et chaque ligne du dépôt n'ont pas fait l'objet d'une revue exhaustive.

**Verdict : conserver le socle ; prioriser le contexte compte/dépôt/droits et rendre exécutables les opérations PR existantes.** C'est là que se trouve le travail répétitif actuellement laissé aux chats.
