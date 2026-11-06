# Améliorations à étudier

## Vérification automatique des projets en fin de tour

**Statut : à étudier, mise de côté à la demande de Guillaume.** Aucun hook de validation automatique n’a été ajouté pour cette proposition.

Objectif : économiser les tokens consacrés au lancement, à l’attente et à la collecte des tests, sans modèle décisionnel supplémentaire ni perte de validation.

### Comportement attendu

- Un hook commun de fin de tour appelle Atelier Routines lorsque les sources ont changé. Il ne contient aucune commande propre à un projet.
- Atelier Routines retrouve les commandes de validation dans le dépôt : scripts déclarés, configuration des outils et workflows CI applicables. La technologie seule ne suffit pas pour choisir une commande.
- Réutiliser les commandes et prérequis existants, sans configuration manuelle supplémentaire pour les cas reconnus. Une commande ambiguë, un projet non pris en charge ou un service requis absent produit un blocage explicite.
- Exécuter les contrôles compatibles Linux sur Atelier VPS, sous les gardes et budgets existants ; conserver les opérations spécifiques à macOS en local.
- Attendre, récupérer et vérifier les résultats, puis fournir un bilan compact avec les erreurs utiles et les références des preuves. L’agent reprend la main si une correction est nécessaire.

### Conditions de fiabilité

- Ne pas lancer une suite après chaque édition ; déclencher à la fin d’un ensemble de modifications. Le hook `Stop` couvre la fin de tour ; une étape intermédiaire demanderait un appel explicite.
- Éviter les exécutions concurrentes et doublons. Préserver les références des calculs interrompus ou incertains, sans nouvelle soumission automatique.
- Associer chaque validation à la version exacte des sources, aux entrées, à l’environnement et au périmètre des contrôles. Invalider le résultat si ces éléments changent.
- Ne pas considérer un contrôle partiel, indisponible, expiré ou en échec comme une réussite. Préserver les suites obligatoires de CI et les assertions E2E réelles.
- Respecter les racines autorisées et les protections existantes. La découverte d’une commande ne constitue pas une autorisation d’exécuter arbitrairement du contenu du dépôt ni de contourner une permission.
- Prévenir les boucles de continuation. L’activation d’un nouveau hook reste soumise à la revue de confiance de Codex.

### Évaluation avant généralisation

Comparer la méthode actuelle et la routine sur plusieurs projets et technologies : tokens réels d’entrée et de sortie, cache distingué, durée, nombre d’interventions du LLM et résultats des contrôles. Mesurer aussi le coût du hook lorsqu’aucune vérification n’est nécessaire.

Inclure des variations de tâches et les cas suivants : tests réussis ou échoués, code modifié pendant le calcul, absence de tests, monorepo, services manquants, interruption et doublons. Ne retenir la solution qu’avec une économie mesurée et aucune régression observée sur le périmètre testé ; cela ne garantit pas tous les cas futurs.

### Point de départ connu

Lors de l’inspection, `checks.regression` dispose d’un adaptateur exécutable pour Atelier Routines uniquement. La découverte et l’exécution pour les autres projets restent à concevoir et à valider.

Référence : [hooks Codex, événement Stop](https://learn.chatgpt.com/docs/hooks#stop).
