# d1 local — rapport rapide et intégration

## Économie estimée

**Ordre de grandeur pour ton usage global :5 à10 % de tokens**, si les décisions routables représentent20–40 % des tokens actuels. Cette part de ton usage n'est pas mesurée ; ce n'est ni une économie garantie ni une baisse de facture démontrée.

Sur le benchmark1000 cas, avec confiance minimale0,99, d1 peut prendre240 décisions et éviter3,94 millions de tokens Sol, soit24,2 % des tokens de ce lot. Aucune erreur sur ces240 décisions n'a été observée, sans garantie hors échantillon. À0,95, l'évitement monte à47 %, avec8 erreurs locales acceptées. Départ retenu :0,99.

Les1000 cas sont complets : d1 BF16899/1000, Sol low902/1000, Sol high902/1000. d1 combine775 cas CPU et225 cas Metal ; sa latence agrégée ne représente pas un seul serveur. Sol passe par Codex et inclut son contexte/cache. Le gain net réel doit aussi déduire le pilotage LLM et la préparation des requêtes ; il peut être inférieur, voire nul sur une petite tâche. Le corpus public anglais mesure des choix fermés et de la lecture de graphiques, pas l'exécution autonome d'outils ou tes tâches métier réelles.

Sur250 cas Mac M2/24 Go : médiane417 ms, p951953 ms,164 cas sous1 s. Sur les mêmes100 cas : BF1684/100 contre Q482/100 ; médianes418/407 ms,68 cas sous1 s dans les deux profils. Le gain Q4 observé est surtout la mémoire ; le16 bits reste retenu. Voir le [rapport expérimental complet](/Users/guillaumegaillard/Documents/ChatGPT/decision-bench/bench1000/RAPPORT-MAC-Q4.md).

## Intégration

Mise à jour du3 novembre : priorité d1 installée dans les instructions globales, les hooks et le skill. Consultation automatique depuis `routines_find` pour les candidats exécutables ambigus, sans exécution de routine. Sortie `local_decision`, seuil0,99, repli si indisponible ou insuffisant. Les anciennes connexions MCP nécessitent une reconnexion pour charger le routage automatique ; les consignes imposent entre-temps l'appel explicite `decision.evaluate` avant Sol.

- Service utilisateur installé : `fr.atelier.routines.d1`, localhost authentifié, Metal, poids BF16 déjà vérifiés, aucun doublon des poids. Démarre à la connexion ; pas de boucle automatique de redémarrage. Les fichiers sous `decision-bench/bench1000/mac-assets` restent nécessaires au service.
- Deux outils MCP inchangés : `routines_find` et `routines_run`. Quatre routines : `decision.status`, `decision.evaluate`, `decision.prepare-vps`, `decision.result`.
- Entrée par fichier, possibilité de `state_file` et d'une image PNG. Sortie courte : choix, confiance, `usable`, `fallback_required`, latence et empreintes. Le contexte brut n'est pas remis dans le chat ni dans le reçu. Pas d'appel Sol automatique. `status=ok` confirme uniquement que l'inférence a été rendue ; il n'autorise pas l'action choisie.
- Déport explicite : paquet fixé sans poids ni sources du projet, arguments prêts pour le MCP Atelier VPS `vps_task`, Préprod uniquement,CPU2/RAM10 Gio, puis fetch/lecture du résultat/libération. Aucune soumission implicite, aucun déplacement après échec et aucun changement de quotas. Les jobs de poids et runtime conservés sont nécessaires au déport.
- Le skill installé guide l'usage seulement pour les décisions ambiguës utiles ; les étapes déterministes et les attentes backend continuent sans modèle.

## Validation acquise

- [Régression de la priorité locale](output/routines-integration/local-first-regression/validation.json) :70 tests,0 échec/erreur et transport exact. [Test réel d'automatisme](output/routines-integration/local-first-installed/local-first.json) : appel automatique274,89 ms, confiance0,9984, compteur d'usage incrémenté, aucune routine exécutée. Les règles déterministes, les gardes et les vérifications ne passent pas par le modèle.

- [Régression complète](output/routines-integration/final-regression/validation.json) :69 tests,0 échec/erreur et benchmark exact de transport réussi, SDK MCP1.30.0. Exécutée sur Mac après refus d'admission Préprod.
- [Installation MCP réelle](output/routines-integration/installed-mcp/installed-decision.json) : texte182 ms, image1373 ms, repli147 ms ; service réel et SDK stdio, sans token cloud. Le repli de faible confiance ne verrouille plus le projet.
- [Sécurité](output/routines-integration/security/stdout.log) : dépendances verrouillées auditées, historique Git vérifié par gitleaks épinglé ; aucun résultat bloquant.
- Tests de contrat : sorties hors candidats et confiance invalide refusées ; chemins protégés/symboliques refusés ; même paquet VPS réutilisé sans écrasement ; résultat importé corrélé à l'empreinte de l'entrée et seuil local réappliqué.
- [Tâche VPS réelle](output/routines-integration/vps-decision/vps-result-hetzner-preprod-4c89ba06fad448ebae0cf0ccb5bd38d9/receipt.json) : MCP `vps_task`, retour0,CPU2/RAM10 Gio réservés, pic RAM4,01 Go, inférence2079 ms. Résultat récupéré avec SHA256, lu par `decision.result`, serveur fermé, tâche libérée. Le MCP a choisi un transfert temporaire de12,4 Kio, sans copier les poids. La première admission avait été refusée ; aucun calcul incertain ou valide n'a été rejoué.

Les deux tentatives de régression distantes en échec (SDK absent au premier appel, délai180 s du préparateur au second) ont été récupérées puis libérées après préservation des preuves. La régression complète finale est passée sur Mac en environ95 s. Les journaux temporaires locaux sont archivés avec leurs reçus ; aucun serveur de test supplémentaire n'est resté actif. Le service d1 installé reste chargé intentionnellement.

Les changements préexistants du dépôt ont été conservés. Aucun commit, push ou merge n'a été fait. La validation GitHub de ces changements n'est donc pas acquise. Les mesures de latence sont illustratives et ne garantissent pas une seconde sur les images ou les longs contextes.
