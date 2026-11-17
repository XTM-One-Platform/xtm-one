# Ce qu'il manque au design system pour que XTM One soit à 100 %

> *Written in French on purpose: this document is for the Filigran design team.
> The engineering counterpart, in English, is [LIBRARY-FEEDBACK.md](LIBRARY-FEEDBACK.md).*

Document à destination de l'équipe design. Il ne liste pas les bugs
d'intégration — ceux-là sont dans [LIBRARY-FEEDBACK.md](LIBRARY-FEEDBACK.md) et
sont du ressort de l'équipe librairie. Il liste **ce qui demande une décision de
design** : des composants à dessiner, des couleurs à trancher, des règles à
poser.

- **Produit :** XTM One
- **Version :** npm `@filigran/design-system@1.1.0`
- **Chiffres :** les couleurs (B1, B4, C1) recomptées le 2026-10-13 (main à
  `e02aca99e`) ; les nombres d'appels des autres sections datent de la
  rédaction et bougent avec `main` — à citer avec leur date.
- **État :** la migration est faite. Zéro token legacy, zéro couleur de palette
  Tailwind, zéro `bg-white` / `text-black`. Ce qui suit est ce qui **ne peut
  pas** être migré, faute d'équivalent dans le langage.

**Comment lire le poids.** Chaque demande porte un nombre d'appels — combien de
fois le produit écrit ce motif aujourd'hui. Ce n'est pas une mesure d'effort
d'implémentation, c'est une mesure de **surface d'exposition** : à 602 appels,
une décision de design se voit partout ; à 30, elle se voit sur un écran.

---

## A. Composants à dessiner

Sept composants n'ont aucune contrepartie. XTM One en garde une version locale,
peinte avec vos tokens — donc les couleurs sont justes, mais l'anatomie, les
états et les proportions sont les nôtres, et elles divergeront.

### A1 · Le chip de statut — 602 appels · le plus lourd du document

`Chip` existe, mais son vocabulaire est la **sévérité** (neutral, info, low,
medium, high, critical) et les familles d'entités CTI. Les badges d'un produit
disent un **statut** : réussi, en attente, en échec, expiré.

Ce ne sont pas la même chose, et les correspondances tentantes sont des
coïncidences de couleur plutôt que de sens : mapper « réussi » sur « low »
étiquetterait un run terminé « faible sévérité », 602 fois.

Deux contraintes qui viennent du code, pas du design :

- **Le nom est pris.** La librairie exporte désormais un `Badge`, mais c'est un
  badge de **compteur** façon MUI (`content`, `max`, `dot`) qui décore une ancre.
  Ce qu'il faut ici est une pastille qui porte un libellé. Il lui faut donc un
  autre nom — `StatusChip`, `Tag`.
- **`Chip` prend `label: string`.** Un badge enveloppe couramment un compteur ou
  une icône. `children` élargirait beaucoup sa portée.

Une chose est déjà réglée et vaut d'être reprise telle quelle : la bonne
recette de contraste est **surface teintée + texte `text-default-primary`**, pas
un texte coloré sur une teinte de sa propre couleur. La première passe de XTM One
a fait la seconde et mesurait 3,73–4,4:1 dans les deux thèmes ; la version
actuelle utilise `bg-alert-*` avec du texte ordinaire par-dessus et dépasse 10:1.

### A2 · L'en-tête de dialogue — 188 appels

`DialogBody` et `DialogFooter` existent, pas de bloc d'en-tête. Le groupe
titre + description + actions reste local sur les 188 appels.

### A3 · L'anatomie de carte — 159 fichiers

`Paper` porte la surface (élévation, rayon, filet) et c'est exactement ce qu'il
faut. Ce qui manque est l'anatomie au-dessus : en-tête, titre, description,
contenu, pied. `Card` est marqué `todo` sur la roadmap.

Une précision utile pour le dessin : XTM One met le padding **sur les parties**,
pas sur le conteneur, parce que ses cartes ont des en-têtes pleine largeur avec
des séparateurs. `Paper` ajoute le sien, d'où un `padding={0}` sur 159 fichiers.
Un `Card` qui suppose le padding sur le conteneur rejouerait le même conflit.

### A4 · Toast / Snackbar — 102 fichiers

Aucune contrepartie, et pas sur la roadmap. Tout produit qui confirme une action
en a besoin.

**Résolu en 1.2.0** : la bibliothèque livre `Snackbar` (`03d41a3`), qu'XTM One
utilise désormais pour toutes ses notifications.

### A5 · Skeleton — 76 fichiers

Idem. Et c'est le composant le plus susceptible de diverger silencieusement :
un skeleton local, c'est un rythme d'animation et une couleur de surface
réinventés par produit.

### A6 · Pagination — 33 fichiers

Idem. Tout écran de liste en a un.

### A7 · État vide — 30 fichiers

Idem. C'est aussi le composant où le ton d'écriture compte le plus, donc celui
qui gagne le plus à être dessiné une fois.

---

## B. Quatre décisions de couleur, mesurées

Ce ne sont pas des préférences. Chaque ligne est un ratio de contraste mesuré
dans un vrai navigateur contre `--bg-elevation-default`.

### B1 · Il n'y a pas d'ambre lisible sur fond clair · majeur

| token | clair | sombre |
|---|---|---|
| `feedback-alert-primary` | **1,54:1** | 11,3:1 |
| `feedback-warning-primary` | 4,32:1 | 6,19:1 |
| `feedback-success-primary` | 4,98:1 | 6,37:1 |
| `feedback-error-primary` | 5,92:1 | 5,18:1 |

`alert` est le token naturel pour l'ambre, et en thème clair il est
**invisible**. Le contournement de XTM One sépare par usage — le texte prend
`warning`, les surfaces gardent `alert` — mais ce n'est pas une correction :
`warning` à 4,32:1 rate encore AA pour du texte normal. Le partage est en
production (au 2026-10-13, hors tests) : 786 classes `feedback-warning-*`, 524
`feedback-alert-*`.

**La demande :** un jaune/orange accessible pour du texte sur surface claire. Le
langage n'en a pas.

### B4 · Le texte posé sur un aplat qui ne change pas de couleur · majeur

L'ambre `feedback-alert-primary` reste le même ambre clair dans les deux thèmes,
et le voile (`scrim`) reste sombre dans les deux. Le texte posé dessus doit donc
lui aussi garder une seule couleur. Le seul candidat du langage,
`text-negative-primary`, est le libellé de l'accent : il **s'inverse** avec le
thème. Juste sur l'accent, faux ici : sur l'ambre, 10,2:1 en sombre mais
**1,5:1 en clair**. Le bandeau d'usurpation d'identité (« vous agissez en tant
que… ») est sorti comme ça, illisible en clair. Aucun passage navigateur ne l'a
vu : il n'apparaît que pendant qu'un administrateur usurpe un compte.

XTM One définit localement `on-feedback-fill` (gray-900) et `on-scrim` (blanc),
tenus à 4,5:1 dans les deux thèmes par un test. Sur l'orange
`feedback-warning-primary` en clair, aucune couleur n'atteint 4,5:1 : le badge
est passé à l'ambre.

**La demande :** un token de texte `on-*` pour chaque aplat qui garde sa
couleur d'un thème à l'autre, à côté des libellés qui s'inversent.

### B2 · Le survol du bouton primaire rate AA en sombre · majeur

Le libellé est `--text-negative-primary`, du texte foncé en thème sombre. Le pas
de survol assombrit la surface sous lui, d'un cran de trop :

| mode | repos | survol |
|---|---|---|
| Copilot (`brand-*`) | blue-400 — **9,32:1** ✓ | blue-700 — **3,6:1** ✗ |
| XTM One (`ia-*`) | purple-300 — 5,55:1 ✓ | purple-500 — **2,41:1** ✗ |

Le repos est confortable dans les deux ; seul l'état interactif échoue. Le thème
clair n'est pas concerné.

Deux choses rendent ça corrigeable plutôt qu'à tolérer. Ça **ne peut pas** être
réglé en changeant la couleur du libellé : sur blue-700, le texte foncé est à
3,6:1 et le clair à 4,36:1 — aucun ne passe, c'est la surface qui doit bouger. Et
un seul cran suffit : blue-600 passe à 5,79:1 en gardant le sens
d'assombrissement voulu. Le violet, lui, n'a aucun pas plus sombre qui passe
(purple-400 est à 3,54:1) : là, le survol doit **éclaircir**.

XTM One surcharge les deux depuis l'hôte et supprimera la surcharge le jour où
ça atterrit.

### B3 · `Switch` est marqué WCAG-failing en amont · mineur

137 appels dans XTM One. Ce n'est pas une demande de correction dans le produit —
c'est une demande de **date**, parce que la posture d'accessibilité du produit
dépend maintenant de la vôtre.

---

## C. Ce que le langage ne modélise pas

### C1 · Pas de palette de data-visualisation ni de graphe · majeur

Le langage modélise le feedback, les entités et l'élévation. Il ne modélise pas
une **série** (un graphe à n séries comparables) ni un **vocabulaire de nœuds**
(types de nœuds, états d'arêtes, fonds de canvas). Les deux sont inévitables
ici : XTM One embarque six composants de graphiques, un flow builder, un canvas
d'agents et un graphe de traces.

C'est là que se trouve la plus grande part de la dette de couleur restante. Sur
117 littéraux de couleur encore en dur (au 2026-10-13), **56 sont exactement
ça** : `flow-builder.css` (27), `node-visuals.ts` (10), `agent-flow.tsx` (9),
`graph-utils.ts` (6), `trace-graph.tsx` (4). Des teintes choisies à la main pour
que deux nœuds voisins restent distinguables, avec une contrepartie thème clair
construite à la main pour chacune. Ce ne sont pas des tokens écrits
paresseusement — il n'y a rien pour les écrire.

Deux propriétés comptent, et aucune n'est satisfaisable avec les tokens actuels :
les séries doivent rester distinguables **entre elles** (pas seulement lisibles
sur le fond), et elles doivent tenir dans les deux thèmes sans être re-choisies à
la main. Une rampe catégorielle générée de 8 à 12 pas, avec une résolution claire
et une sombre, fermerait la question.

Petit parent du même manque : une **teinte d'accent décorative** à faible alpha,
pour les halos et les lavis de dégradé, apparaît plus de 20 fois sous la forme
`hsl(197 100% 53% / 0.15)` et voisines. Elles ne peuvent pas être des tokens
aujourd'hui parce qu'elles doivent suivre l'accent runtime — voir C3.

Le reste des littéraux est **correctement** hors du langage, et c'est noté ici
pour qu'on ne le prenne pas pour de la dette : 12 couleurs de marque tierces
(Slack, Teams, WhatsApp, Telegram, Discord, Google), et la formule de dérivation
de la palette white-label, qui calcule des couleurs au lieu de les nommer.

### C2 · La famille `content-*` s'arrête à 14 px · majeur

Les styles composites se partagent proprement par rôle : `content-*` pour le
corps en IBM Plex, `title-*` pour les titres en Geologica. Mais le plus grand pas
du corps est `content-base` à 14 px, alors que `title-sm` — le premier pas de
titre — est déjà à 16 px. **Il n'y a pas de style de corps au-dessus de 14 px.**

Tout produit avec un chapô, une valeur de statistique en grand ou une phrase mise
en avant doit donc soit emprunter un `title-*` (mauvaise famille, mauvaise
graisse), soit sortir du système. XTM One est sorti du système, 207 fois :

| taille app | px | pas FDS le plus proche | verdict |
|---|---|---|---|
| `text-base` | 16 | `title-sm` (16 px, Geologica bold) | bonne taille, mauvaise famille · 65× |
| `text-lg` | 18 | — (l'échelle saute 16 → 20) | **aucun pas n'existe** · 24× |
| `text-xl` | 20 | `title-md` (20 px) | utilisable si un titre convient · 10× |
| `text-2xl` | 24 | `title-lg` (24 px) | utilisable, même réserve · 48× |
| `text-3xl` | 30 | — (l'échelle saute 28 → 32) | **aucun pas n'existe** · 60× |

Deux pas `content-lg` et `content-xl` — famille corps, 16 et 18 px — fermeraient
l'essentiel.

### C3 · L'accent hôte, et le white-label qui n'atteint pas les dégradés · majeur

Un déploiement peut être rebrandé par son opérateur, et le produit lui-même
existe en deux identités : Copilot bleu, XTM One violet. Aucune des deux ne peut
s'exprimer dans un token de la librairie — les tokens couleur sont générés depuis
Figma et volontairement non éditables, et la couleur d'un opérateur est une
valeur runtime qu'aucun token généré ne peut nommer.

XTM One s'en sort avec un token produit, `--color-accent`, résolu dans l'ordre :
couleur opérateur → mode produit → défaut. Cette moitié-là fonctionne.

C'est l'autre moitié qui pose problème. **Vos composants peignent depuis
`--color-filigran-brand-*` et n'ont aucune notion d'accent hôte**, donc un bouton
primaire reste bleu Filigran sur un déploiement rebrandé — sauf si l'hôte
surcharge un token généré depuis Figma, c'est-à-dire exactement ce qu'on demande
aux consommateurs de ne pas faire. XTM One le fait à deux endroits, sans être à
l'aise : le mode XTM One remappe les trois pas de la famille de marque sur la
famille `ia` (sans quoi le bouton de connexion s'affiche `#0015a8` à côté d'une
identité `#651fe5`), et le white-label injecte la couleur de l'opérateur
par-dessus `--color-filigran-brand-primary` au runtime.

Et une partie reste **hors d'atteinte** : les surfaces peintes depuis les
dégradés de marque — les variantes `ia` et `highlight` du bouton, `bg-gradient-*`.
Ils consomment des tokens multi-arrêts générés, impossibles à reconstruire depuis
un seul hex sans inventer une palette. Un déploiement white-labellisé garde donc
le dégradé Filigran à côté de l'accent de l'opérateur.

**La demande :** une couture d'accent documentée — un token que vos composants
lisent pour leur couleur de marque et qu'un hôte a le droit de poser — plus une
règle explicite sur ce que devient un dégradé quand la marque est surchargée :
dérivé, désaturé en neutre, ou désactivé.

### C4 · Trois valeurs que chaque hôte réinvente · mineur

- **Un voile de modale.** FDS peint l'élévation en couleur plutôt qu'en ombre et
  n'a pas de token pour l'assombrissement derrière une modale.
- **Une ombre de survol composée.** `--shadow-sm|md|lg|xl` sont des **rayons** de
  profondeur (4/8/16/20 px), pas des `box-shadow`. Un menu ou un popover qui
  flotte au-dessus du contenu doit quand même s'en détacher.
- **Des piles de repli de police.** Les tokens typographiques nomment une famille
  et s'arrêtent. Si la webfont n'arrive pas — déploiement air-gapped, CDN bloqué,
  premier paint lent — le navigateur retombe sur son propre défaut, un serif dans
  la plupart des cas, et **toute l'application s'affiche en Times**.

---

## D. Quatre arbitrages de comportement

### D1 · La page d'accueil d'un sous-menu est injoignable quand il est déplié

Le `to`/`href` de `NavbarSubmenu` ne s'applique **qu'en mode replié**, au motif
qu'« un intitulé de groupe n'est pas lui-même une page distincte quand il y a la
place d'afficher ses enfants ». Dans XTM One, c'en est une : `/agent-studio` et
`/tools-and-integrations` sont des pages de hub avec leurs propres actions de
création, et on y accédait en cliquant l'intitulé du groupe.

Adopter le composant tel quel aurait **silencieusement supprimé** cette route de
l'interface. Le contournement est une ligne « Vue d'ensemble » ajoutée dans
chaque sous-menu : ça marche, mais c'est une entrée de menu inventée pour
contourner une décision de composant. Une prop (`triggerNavigates`) trancherait.

### D2 · `Navbar` n'a pas de fente pour un intitulé de section

La navigation a des sections libellées (« Ressources ») au-dessus de groupes
d'entrées. `NavbarSeparator` trace un trait sans libellé, et `NavbarSubmenu`
ajoute un chevron et un comportement de dépliage qu'un intitulé ne doit pas
avoir. XTM One rend un `<p>` stylé avec vos tokens.

### D3 · Les largeurs de `Dialog` ne rencontrent pas un consommateur à mi-chemin

Les tailles sont des largeurs fixes (420/640/960 px). XTM One plafonne ses
dialogues avec `max-w-*` sur **59 appels sur 63**, et la largeur fixe l'emporte
vers le bas : un dialogue `max-w-3xl` devenait 640 px sous `size="md"`. Une
taille `auto` — ou le respect d'un `max-width` entrant — répondrait directement.

### D4 · La doctrine « toujours fixe » de `Header`

Le composant ne porte aucun positionnement et sa RFC énonce que la barre est
toujours `fixed`, jamais `sticky`. XTM One garde `sticky`, parce que `fixed`
déplace le défilement dans la zone de contenu et change d'un coup la restauration
de scroll, les en-têtes collants intra-page et la géométrie du panneau assistant.
C'est un changement à faire pour lui-même, pas comme effet de bord de l'adoption
d'un composant. Noté ici plutôt que de feindre la conformité.

---

## Ce qui marche, et qu'il ne faut pas toucher

- **Le modèle d'élévation.** L'échelle d'opacité en six pas de XTM One
  (`text-muted-foreground/70…/20`) et ses surfaces de survol `bg-foreground/5`
  étaient des bricolages qui remplaçaient exactement ça. Les ramener à trois
  niveaux de texte et quatre calques est une simplification, pas une perte.
- **`--fds-header-height`.** Publier la hauteur de la barre en propriété
  personnalisée, avec le défaut dans le repli du `var()`, est exactement la
  bonne forme : la barre, le décalage du contenu et le panneau assistant ne
  peuvent plus diverger. **Les largeurs de la navbar mériteraient le même
  traitement** — `w-45`/`w-12` sont internes, et le consommateur doit
  re-saisir 180/48 px à la main.
- **`aria-current="page"` comme mécanisme de sélection**, plutôt qu'une prop. Le
  routage reste la source de vérité unique.
- **Le double mode de `NavbarSubmenu`** a supprimé ~120 lignes de flyout fait
  main, sa gestion d'intention de survol et son câblage d'Échap.
- **`IconButton` qui exige un libellé accessible.** C'est ce qui a révélé que 53
  des 58 boutons-icônes de XTM One n'avaient aucun nom accessible. La contrainte
  a fait son travail.

---

## Annexe — ce qui ne relève pas du design

Listé pour qu'on ne le croie pas oublié. Détail dans
[LIBRARY-FEEDBACK.md](LIBRARY-FEEDBACK.md).

| # | Sujet | Sévérité |
|---|---|---|
| F19 | Une icône passée en enfant de `Button` part à la ligne dans tout hôte avec un preflight | bloquant |
| F01 / F02 | Yarn refuse les dépendances git ; un consommateur durci installe le paquet vide | bloquant |
| F03 / F21 | Les hôtes Tailwind v4 ne compilent pas les classes de la librairie ; le contournement n'a pas de borne | majeur |
| F09 | Les logos produits sont déclarés mais absents de la surface d'export | majeur |
| F10 | `Input` restreint `type` et `value` en deçà de ce que l'élément accepte | majeur |
| F20 | Le garde-fou CI ne connaît pas `docker compose build` | majeur |
| F00 / F04 / F05 | Nom du secret ; `--color-*: initial` non documenté comme cassant ; collision `--shadow-*` | mineur |
