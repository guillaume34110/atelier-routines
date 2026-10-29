# Sécurité

Signaler une vulnérabilité via [le formulaire privé GitHub](https://github.com/guillaume34110/atelier-routines/security/advisories/new), sans publier de secret dans une issue.

Le serveur est un outil local, pas un bac à sable du système d’exploitation. Son installation et son code sont de confiance. Les recettes des agents sont des données non exécutables : opérations autorisées, schémas stricts, projet imposé, pas de shell, de code évalué, de récursion ou d’élargissement des permissions. Les clients externes restent ceux installés et autorisés par l’utilisateur.

Les paramètres de fichiers restent relatifs à une racine autorisée. Les lectures contrôlent chaque composant sans suivre de liens ; fichiers non réguliers, liens physiques, chemins protégés et fichiers de secrets courants sont refusés. La recherche énumère seulement les chemins avec rg, puis ouvre les fichiers avec ces contrôles ; elle ne suit pas les fichiers d’ignore fournis par le projet. `CLAUDE.md` n’est jamais ouvert. Les inspections Git désactivent fsmonitor, pager, signatures et diff/textconv externes.

Les workers ne reçoivent pas les jetons de connecteurs ou variables d’injection Python. Le client VPS utilise ses autorisations locales existantes ; clés, configurations personnelles et historique Git ne sont pas transférés. Les requêtes HTTP autonomes sont limitées à des endpoints de santé fixes sur 127.0.0.1, sans identifiants, corps retourné ni redirections.

Les opérations courtes sont limitées à 15 s. Les fichiers sont limités à 8 Mio, l’AST MCP à 256 Kio, la recherche à 32 Mio / 5 000 fichiers UTF-8 de 2 Mio maximum. Les captures et reçus sont bornés ; les indices de comparaison sont limités à 500 erreurs distinctes. Un index incomplet empêche une comparaison présentée comme complète.

Les reçus privés (0600, dossier 0700) sont sous `PROJECT/output/routines-evidence`, avec verrou et plafond de 16 Mio / 1 024 reçus. Le registre est borné à 100 recettes / 1 Mio ; le suivi à 1 024 exécutions / 16 Mio. Les sorties temporaires utilisent les gardes Atelier et workspace-budget. Atteindre une limite arrête ou refuse l’opération ; aucune purge de données personnelles ni relance automatique ne la contourne. Les historiques sont conservés pour une décision explicite d’archivage.

Le retour vise moins de 2 000 octets, avec chemins de preuves et `response_truncated` si nécessaire. Les comptes d’un scan partiel sont des bornes inférieures. Les extraits masquent des formes courantes de secrets ; ce masquage est heuristique. Sélectionner uniquement des données autorisées à être transmises au modèle. Les valeurs JSON/CSV, arguments des processus, identifiants de remotes et corps des endpoints de santé ne sont pas exposés.

Un état `running`, `guided`, `unavailable`, `partial`, `error`, `timeout`, `decision_required` ou `abandoned` n’est pas une validation réussie. Le succès d’une collecte, d’une assertion de rapport ou d’un endpoint de santé ne prouve pas un E2E. Un appel interrompu ne relance pas le calcul ; une reprise VPS ne fait qu’attendre ou collecter une référence connue. Une preuve absente, altérée ou incompatible ne vaut jamais succès.

Le dépôt public exige une PR, les contrôles verify/security à jour et les mêmes règles pour les administrateurs. Les workflows ont un jeton en lecture seule, ne persistent pas les identifiants de checkout et utilisent des versions/empreintes épinglées. Les alertes de dépendances, la détection de secrets et la protection des pushes complètent les vérifications locales ; elles ne garantissent pas l’absence de toute vulnérabilité.
