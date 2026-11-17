# Rokh Solis : analyse d'un mode opératoire informationnel ayant ciblé les élections municipales de mars 2026 (VIGINUM, juin 2026)

Source : site internet du secrétariat général de la défense et de la sécurité nationale (SGDSN), https://www.sgdsn.gouv.fr/viginum/publications/rokh-solis-analyse-dun-mode-operatoire-informationnel-ayant-cible-les  
Document original : https://www.sgdsn.gouv.fr/files/files/viginum/Publications/20260611_SGDSN_VIGINUM_Rapport-technique_Rokh-Solis.pdf

> Document enrichi pour la base de connaissances : le texte du PDF est complété, à l'emplacement de chaque figure, par une annotation qui décrit son contenu (entités, relations orientées, valeurs) et la règle qu'elle illustre. Les blocs « Figure » sont des descriptions rédigées à partir des images, pas du texte original.

> Note : Rapport technique de 41 pages PDF ; la pagination imprimée du document est décalée de 2 par rapport aux pages PDF (page imprimée 1 = page PDF 3), et les marqueurs « p. » des figures ci-dessous sont les pages PDF. Les annexes 5.3 (listes de comptes et de groupes Facebook), 5.4 (cluster de comptes X) et 5.6 (cluster LinkedIn) sont des tableaux à couche texte, déjà présents dans le texte extrait ; seuls les schémas 5.1, 5.2 et 5.5 sont annotés. Les captures montrent des pages de désinformation ciblant des personnalités politiques : les accusations visibles sont décrites comme des contenus diffusés par le dispositif, jamais comme des faits. Le texte du rapport donne l'adresse IP 198.177.120[.]188 pour les sites blogdesophie.com et lalternative2026.com (note 2) alors que le schéma de l'annexe 5.2 affiche 198.177.120.194 et 198.177.120.196 ; la divergence est reportée telle quelle.

<!-- page 1 -->
<!-- page 2 -->
<!-- page 3 -->
Table des matières

Synthèse .................................................................................................................................................. 2

1. Narratifs et contenus manifestement inexacts ou trompeurs ............................................. 3 1.1. Dénigrement du parti politique LFI et de ses candidats ........................................................................... 3 1.2. Polarisation du débat public numérique via l’instrumentalisation de la communauté musulmane4

2. Dispositif de diffusion artificielle ou automatisée, massive et délibérée........................... 5 2.1. Ciblage des candidats LFI pour les mairies de Marseille, Toulouse et Roubaix.................................... 5 2.1.1. blogdesophie.com ................................................................................................................................. 5 2.1.2. delogupourpalestine.com .................................................................................................................... 7 2.1.3. piquemalzero.com ................................................................................................................................. 7 2.1.4. Usurpation de l’identité de l’association « Enfance et Partage » sur Facebook ....................... 8

2.1.5. « La vérité cachée sur David Guiraud » ............................................................................................ 9 2.1.6. Relais inauthentiques sur Facebook potentiellement impliqués dans d’autres opérations ... d’ingérences numériques étrangères .............................................................................................. 10

2.2. Mise en scène de l’opposition entre un prétendu écosystème islamiste radical et une soi-disant association souverainiste ..................................................................................................................................... 15 2.2.1. lalternative2026.com .......................................................................................................................... 15 2.2.2. rcn-france.org, un cinquième site alertant sur l’existence de lalternative2026.com ............. 16

2.2.3. forsane-alizza.eu, un sixième site lié à l’écosystème lalternative2026.com ............................. 17

3. Implication d’un acteur étranger ............................................................................................ 20

3.1. Des relais inauthentiques communs avec une organisation pro-israélienne..................................... 20 3.2. Des éléments techniques comportant certains marqueurs d’extranéité........................................... 21

3.3. Une entreprise israélienne probablement à l’origine des campagnes ............................................... 22

3.3.1. Blackcore, une entité israélienne sans statut légal dont les stratégies se rapprochent des campagnes observées ...................................................................................................................................... 22 3.3.2. Une infrastructure révélant un large éventail d’outils d’amplification artificielle liés aux opérations ciblant les élections municipales .............................................................................................. 23 3.3.3. Des liens avec un business angel israélien ..................................................................................... 28

4. Atteinte aux intérêts fondamentaux de la Nation ............................................................... 29

5. Annexes ....................................................................................................................................... 30

<!-- page 4 -->
SYNTHÈSE

En mars 2026, VIGINUM a détecté l’existence d’un nouveau mode opératoire informationnel (MOI), baptisé Rokh Solis, s’articulant autour d’un réseau de sites web et de comptes de réseaux sociaux visant à dénigrer le parti politique La France Insoumise (LFI), ainsi que plusieurs de ses candidats aux élections municipales des 15 et 22 mars 2026. Les investigations menées par VIGINUM démontrent que la campagne conduite au moyen de ce MOI réunit les critères d’une ingérence numérique étrangère. Quatre des sites web en question – blogdesophie.com, delogupourpalestine.com, piquemalzero.com et lalternative2026.com – présentent des caractéristiques techniques communes (même adresse IP, date d’enregistrement, métadonnées des images, etc.) et bénéficient de relais coordonnés sur TikTok, Instagram, X et Facebook par des comptes présentant plusieurs marqueurs d’inauthenticité. En outre, les analyses réalisées sur le site lalternative2026.com et sur des comptes associés au MOI Rokh Solis permettent de rattacher deux autres sites à ce même écosystème : forsane-alizza.eu (affichant son soutien à lalternative2026.com), et rcn-france.org (qui, au contraire, alerte sur l’existence de lalternative2026.com). Résolument hostile, la ligne éditoriale de cet écosystème s’inscrit dans une stratégie de discrédit général de La France Insoumise, et de dénigrement de ses candidats et députés Sébastien DELOGU, François PIQUEMAL et David GUIRAUD, respectivement à Marseille, Toulouse, Roubaix. Par ailleurs, le second volet du dispositif (rcn-france.org et forsane-alizza.eu) met dos à dos deux positionnements idéologiques antagonistes avec d’un côté, un mouvement à la ligne conservatrice, identitaire et souverainiste et, d’un autre côté, un pseudo-groupe radical islamiste, visant à polariser le débat public national en instrumentalisant la communauté musulmane. Au-delà du champ numérique, un compte TikTok associé à rcn-france.org a fait la promotion d’actions dans la sphère physique à Paris. Une opération similaire, notamment via la mise en valeur de collages à Marseille, a été promue par le compte X @LeBlogdeSophie. Malgré la variété d'actifs numériques, de nombreuses tentatives d'amplification artificielle et la latéralisation des opérations d'ingérences numériques étrangères sur plusieurs plateformes, les opérations du MOI Rokh Solis n'ont bénéficié que d'une faible visibilité en ligne en amont des élections municipales 2026. Enfin, s’agissant de l’implication d’un acteur étranger, VIGINUM a relevé plusieurs marqueurs techniques d’extranéité : présence de caractères hébreux dans les métadonnées d’un site, localisation en Israël du compte TikTok @renaissancecatholiquefr, et partage de hashtags et de publications associés à l’organisation pro-israélienne ELNET, indiquant l’implication, a minima indirecte, d’un acteur étranger.

Plusieurs éléments techniques ont orienté les investigations de VIGINUM vers une entité privée israélienne nommée Blackcore, dont les stratégies d’influence numérique s’apparentent à celles observées dans le cadre des campagnes informationnelles conduites au moyen du MOI Rokh Solis. Les investigations menées sur cette entreprise mettent en lumière un ensemble d’outils d’amplification artificielle sur les réseaux sociaux, inséré au sein d’un écosystème d’entités privées israéliennes de cybersécurité. À ce stade, l’étendue de cet environnement ne permet pas l’identification précise et la qualification du rôle exact des opérateurs à l’origine du MOI Rokh Solis.

<!-- page 5 -->
1. NARRATIFS ET CONTENUS MANIFESTEMENT INEXACTS OU TROMPEURS

Les opérateurs du MOI Rokh Solis semblent avoir mobilisé deux stratégies narratives distinctes visant à dénigrer le parti politique La France Insoumise (LFI) et certains de ses candidats aux élections municipales 2026, ainsi qu’à polariser le débat public numérique autour de thèmes clivants, en particulier en accusant le parti LFI de communautarisme musulman. Ces deux stratégies narratives ont donné naissance à plusieurs écosystèmes d’actifs numériques dont le déploiement, le comportement et certaines mesures d’amplification présentent de nombreux marqueurs de coordination et d’inauthenticité (cf. partie 2).

1.1. Dénigrement du parti politique LFI et de ses candidats L’analyse de la ligne éditoriale des différents actifs numériques mobilisés par les opérateurs de Rokh Solis permet de mettre en lumière leurs objectifs : porter atteinte à la réputation de LFI et de certains de ses candidats et candidates, et polariser le débat public national en instrumentalisant la communauté musulmane. D’une part, la publication d’une liste de candidats de La France Insoumise ou affiliés, sur le site lalternative2026.com, s’inscrit dans une stratégie de discrédit général du parti. Cette approche repose notamment sur l’accusation de communautarisme portée contre LFI, présenté comme une caractéristique centrale de la politique que le parti chercherait à promouvoir, aussi bien au niveau local que national.

D’autre part, des attaques ciblées contre certains candidats spécifiques de LFI aux élections municipales ont été menées pour nuire à leur réputation et renforcer la critique globale contre le parti. Ainsi, par le biais de narratifs manifestement inexacts ou trompeurs soutenant des accusations de crimes, trois candidats LFI ont été directement pris pour cible par les principaux actifs numériques du MOI Rokh Solis : Sébastien DELOGU, François PIQUEMAL et David GUIRAUD, respectivement candidats aux élections municipales de Marseille, Toulouse et Roubaix.

> **[Figure F1 - p. 5] Tableau**
>
> Tableau de synthèse des narratifs du MOI Rokh Solis (actifs numériques, cible, narratif diffusé). Les accusations ci-dessous sont celles diffusées par le dispositif, que le rapport qualifie de narratifs manifestement inexacts ou trompeurs.
>
> | Actifs numériques du MOI | Cible | Narratif diffusé |
> |---|---|---|
> | Le site blogdesophie.com, les comptes X et Facebook « Le Blog de Sophie » | Sébastien DELOGU | Accusation de viol. |
> | Le site delogupourpalestine.com | Sébastien DELOGU | Présentation d'une lettre prétendument rédigée par le candidat LFI qui proposerait à la vente un calendrier mettant en scène des photos de sa personne dont certaines seraient dénudées, afin de « consacrer la vente de [ses] photographies à un soutien direct en faveur de la population de Gaza ». |
> | Deux pages Facebook et le site piquemalzero.com | François PIQUEMAL | Accusation de pédophilie, de violence et de soutien envers les actes terroristes du Hamas, usurpation de l'identité d'une association de protection de l'enfance. |
> | Deux pages Facebook | David GUIRAUD | Dénonciation des prises de positions propalestiniennes dans le cadre du conflit qui oppose Israël au Hamas. |
>
> Ce que la figure enseigne : chaque candidat LFI ciblé (Marseille, Toulouse, Roubaix) reçoit un narratif et un jeu d'actifs dédiés (un site et/ou des pages Facebook), avec des accusations de crimes pour Delogu et Piquemal et une charge politique propalestinienne pour Guiraud ; le tableau donne la correspondance actif - cible - narratif utile pour l'imputation au MOI.

Actifs numériques du MOI Cible Narratif

Le site blogdesophie.com, les Sébastien comptes X et Facebook Accusation de viol. DELOGU « Le Blog de Sophie » Présentation d’une lettre prétendument rédigée par le candidat LFI qui proposerait à la vente un calendrier Le site Sébastien mettant en scène des photos de sa personne dont certaines delogupourpalestine.com DELOGU seraient dénudées, afin de « consacrer la vente de [ses] photographies à un soutien direct en faveur de la population de Gaza ». Accusation de pédophilie, de violence et de soutien envers Deux pages Facebook et le site François les actes terroristes du Hamas, usurpation de l’identité d’une piquemalzero.com PIQUEMAL association de protection de l’enfance.

David Dénonciation des prises de positions propalestiniennes dans Deux pages Facebook GUIRAUD le cadre du conflit qui oppose Israël au Hamas.

<!-- page 6 -->
1.2. Polarisation du débat public numérique via l’instrumentalisation de la communauté musulmane Pour crédibiliser le déploiement du site lalternative2026.com, ciblant aussi indirectement le parti LFI, les opérateurs du MOI Rokh Solis ont créé deux écosystèmes numériques distincts et aux narratifs antagonistes. Le premier écosystème s’inscrit autour d’un prétendu projet, « L’Alternative 2026 », incarné par le nom de domaine lalternative2026.com. Celui-ci fait la promotion d’une démarche politique communautariste en faveur de la population musulmane dans le cadre des élections municipales, en citant une liste de candidats LFI présentés comme favorables à ce programme. Ce discours revendique ainsi des éléments comme la mise en place d’un système juridique religieux ou la facilitation du port de signes religieux. L’objectif présenté sur lalternative2026.com serait « d’aider les citoyens musulmans à identifier, dans chaque ville, le candidat qui représentera le mieux ses intérêts ». La juxtaposition de ces principes auprès d’une liste de candidats LFI ou affiliés suggère insidieusement une complicité entre le parti et les revendications communautaristes de ce projet. De plus, une autre partie du même écosystème avait pour but de promouvoir « L’Alternative 2026 ». Pour ce faire, les opérateurs du MOI ont créé des actifs numériques empruntant le nom d’un ancien groupe radical islamiste aujourd’hui dissous, « Forsane Alizza 1 » : ces actifs étaient destinés à véhiculer des narratifs inspirés du socle idéologique salafiste de ce groupe, mais également à saluer l’initiative en faveur de la communauté musulmane en France portée par lalternative2026.com.

La construction de cette posture communautaire a ensuite facilité la mise en place d’un second écosystème antagoniste. Articulé autour d’une prétendue association dénommée « Renaissance catholique Nationale », à la ligne souverainiste, identitaire, catholique et conservatrice, cet écosystème a diffusé un discours alarmiste sur le « déclin de la France » et a averti contre « la montée de l’islamisme radical », notamment via la dénonciation du site lalternative2026.com. Cette dialectique narrative a probablement pour objet de susciter de l’engagement en ligne, afin de faire monter en visibilité ces deux sphères et espérer in fine polariser le débat public numérique sur la question de la place de la communauté musulmane dans la société française.

Forsane Alizza ou « Cavaliers de la Fierté », était un groupe radical islamiste se présentant sous le statut d’une association depuis

2010. Le groupe était constitué d’une quinzaine d’individus actifs en France entre 2010 et 2012. Les activités de ce groupe se structuraient principalement autour d’actions en ligne et de manifestations dans la sphère physique, sur des thématiques essentiellement antisémites et anti-blasphématoires.

<!-- page 7 -->
2. DISPOSITIF DE DIFFUSION ARTIFICIELLE OU AUTOMATISÉE, MASSIVE ET DÉLIBÉRÉE

Entre les 9 et 19 février 2026, VIGINUM a détecté la création des quatre sites web conçus pour cibler le parti LFI et certains de ses candidats dans le cadre des élections municipales 2026. Ces quatre sites possèdent des caractéristiques techniques communes, permettant d’affirmer avec un niveau de confiance élevé qu’ils forment un seul écosystème, potentiellement créé par un même opérateur (cf. annexes 5.1 et 5.2). Ces sites sont : blogdesophie.com, delogupourpalestine.com, piquemalzero.com et lalternative2026.com.

Les investigations conduites par VIGINUM ont notamment permis d’établir que le site blogdesophie.com partageait plusieurs éléments techniques communs avec le site lalternative2026.com :

- les deux sites ont été déposés le 9 février 2026 et sont hébergés sur la même adresse IP 2 ;

- des images générées par le même grand modèle de langage (LLM) Gemini ont été téléversées sur ces deux sites ;

- une image téléversée sur le site blogdesophie.com comporte le même nom d’auteur (« usman latif ») que huit images présentes sur les sites lalternative2026.com et delogupourpalestine.com. Ces visuels ont tous été générés via l’outil Canva 3. Le déploiement de ces sites s’est notamment accompagné de la création de pages Facebook et de comptes sur X, Instagram et TikTok.

VIGINUM a détecté la présence de deux autres sites associés à cet écosystème : rcn-france.com et forsane-alizza.eu. Comme évoqué supra, le premier incarne la prétendue vitrine d’un mouvement politique français portant une ligne éditoriale conservatrice et souverainiste, dénonçant notamment l’existence du projet soi-disant représenté par lalternative2026.com. Quant au second, il se présente comme la résurgence d’un groupe radical islamiste français, saluant l’initiative en faveur de la communauté musulmane en France portée par lalternative2026.com.

La visibilité des différents actifs numériques précités est restée faible, malgré de nombreuses tentatives d'amplification artificielle.

2.1. Ciblage des candidats LFI pour les mairies de Marseille, Toulouse et Roubaix

2.1.1. blogdesophie.com Le nom de domaine blogdesophie.com 4 a été créé le 9 février 2026 et est protégé par le service d’anonymisation Cloudflare 5. Composé d’une seule page, le site se présente comme le blog d’une prétendue ancienne collaboratrice de Sébastien DELOGU, qui émettrait des accusations de viol à son encontre et chercherait ainsi à briser « le silence » via ce blog. Ce site renvoie également vers un compte

198.177.120[.]188. Les adresses IP réelles des sites du MOI Rokh Solis sont protégées par le service d’anonymisation Cloudflare. Ces sites utilisant la technologie WordPress, il est possible d'envoyer une requête dite « pingback », afin de dévoiler leur adresse IP réelle. En effet, à moins que ces sites n'aient désactivé les « pingbacks », le serveur WordPress renverra directement une réponse à cette requête en contournant le service Cloudflare, dévoilant ainsi leur adresse réelle. Canva Pty Ltd est une entreprise australienne proposant des produits de conception graphique en ligne. https://archive.ph/KebHZ. Les sites lalternative2026.com, delogupourpalestine.com et piquemalzero.com sont hébergés derrière le service d’anonymisation Cloudflare sur le couple de serveurs de noms carol.ns.cloudflare.com et simon.ns.cloudflare.com.

<!-- page 8 -->
X et une page Facebook 6, indiquant tous deux un lien vers le site web dans leur biographie et diffusant des hashtags communs 7, des photos de tracts collés à Marseille, ainsi qu’une vidéo de témoignage de cette supposée victime. Le témoignage indique que le prénom Sophie est un pseudonyme, utilisé par peur des représailles du député de la septième circonscription des Bouches-du-Rhône. Capture d’écran du probable bureau de l’administrateur du site Une capture d’écran du bureau de blogdesophie.com l’administrateur du site semble par ailleurs avoir été téléversée par erreur sur blogdesophie.com, permettant de voir un certain nombre d’outils employés par au moins l’un des opérateurs de cette campagne, ainsi qu’un format de date habituellement non utilisé en France (« 2026/2/12 »).

> **[Figure F2 - p. 8] Capture de bureau Windows (erreur opérationnelle de l'opérateur)**
>
> Capture d'écran du probable bureau de l'administrateur du site blogdesophie.com, téléversée par erreur sur le site (d'après le rapport).
>
> Contenu visible : fenêtre de navigateur de type Chrome avec deux onglets, « Cloudflare Dashboard | Manag… » et « New Tab » ; la barre d'adresse contient le mot « Domains » ; barre de favoris avec « Inbox | blogdesophi… », « Client Area - Orang… » et « Account home | Blo… ». Page d'accueil Google avec un doodle violet et, en raccourcis : « OrangeWebsite », « Orangewebsite… », « Panel », « Proton », « Default Web Sit… », « Inbox », « Mullvad VPN », « Log in », « http://tempmail… », « Add shortcut ». Bouton « Customize Mimic » en bas à droite. Barre des tâches Windows avec l'horloge « 11:45 AM 2026/2/12 ».
>
> Le rapport relève que la capture permet de voir des outils employés par au moins un opérateur et un format de date (année/mois/jour, « 2026/2/12 ») habituellement non utilisé en France.
>
> Ce que la figure enseigne : erreur de sécurité opérationnelle de l'opérateur. Elle trahit l'usage de Cloudflare, d'un hébergeur/registraire (raccourcis « OrangeWebsite »), d'une messagerie Proton, d'un VPN (Mullvad VPN) et d'un service d'adresses temporaires, le tout le 12 février 2026, trois jours après l'enregistrement du domaine (9 février 2026).

La page Facebook, « Le Blog de Sophie », a été créée le 19 février 2026. Entre cette date et le 5 mars 2026, la page a diffusé sept publications, notamment une vidéo publiée le 27 février d’une femme qui témoigne, dos à la caméra et avec une voix modifiée, du supposé viol.

> **[Figure F3 - p. 8] Capture de page Facebook**
>
> Page Facebook « Le Blog de Sophie » (interface en anglais) indiquant le lien vers blogdesophie.com.
>
> Éléments visibles : photo de profil en forme de badge violet avec une silhouette féminine et un mégaphone et le texte « SOPHIE - Le Blog #BalanceTonPorc - Libérons la parole » ; bandeau violet à silhouettes ; nom « Le Blog de Sophie » ; « 490 followers • 0 following » ; onglets Posts / About / Reels / Photos. Bloc Intro : « Je m'appelle Sophie. Ici, je partage comment Sébastien Delogu m'a fait du mal et a détruit ma vie. Suivez ma page et partagez. http://blogdesophie.com ». À droite, une publication de la page datée « 18h » (« Merci beaucoup !… See more ») avec une image.
>
> Ce que la figure enseigne : la page se présente comme le témoignage d'une victime pseudonymisée (« Sophie ») contre Sébastien DELOGU, reprend le hashtag #BalanceTonPorc et renvoie vers le site. La page n'a que 490 abonnés et aucun abonnement, et le rapport indique qu'elle a été amplifiée par un réseau de 500 pages et comptes Facebook inauthentiques.

De plus, VIGINUM a détecté la diffusion de neuf photos de tracts apparemment collés à Marseille 8, sur lesquelles figure un code QR redirigeant vers le site blogdesophie.com. Les tracts photographiés à Page Facebook « Le Blog de Sophie » indiquant le lien Marseille possèdent une charte graphique semblable vers le site blogdesophie.com au tract relayé par le compte TikTok @renaissancecatholiquefr 9 dans une publication du 9 février 2026 (cf. partie 2.2.2). La page Facebook a également diffusé d’autres contenus probablement générés via un outil d’intelligence artificielle générative, ainsi qu’une capture d’écran d’une prétendue conversation avec Sébastien DELOGU. Plusieurs de ces publications semblent avoir été amplifiées artificiellement et de manière coordonnée par un réseau de 500 pages et comptes Facebook, qui présentent des marqueurs d’inauthenticité 10. Le compte X @LeBlogdeSophie0 a quant à lui été créé le 6 mars 2025 et son nom d’utilisateur modifié le 18 février 2026. Entre le 28 février et le 4 mars 2026, les mêmes contenus que sur la page Facebook y sont diffusés 11. À trois reprises, le compte X a interpellé celui de Sébastien DELOGU, en répondant à trois de ses publications.

https://archive.ph/bm9s7 (facebook.com/LeBlogdeSophie0) et https://archive.ph/lBHmA (x.com/LeBlogdeSophie0). #sebastiendelogu, #balancetonporc, #leblogdesophie, #marseillemeritemieux, #marseillevote, #BlogdeSophie. Après investigations, VIGINUM a pu localiser cinq emplacements où les tracts ont été placés : 67 Cours Julien, 13006 Marseille ; 39 Cours Julien, au niveau de « L’Espace Julien » ; 10 rue Pierre Mouren, 13007 Marseille ; 396 rue d’Endoume, 13007 Marseille ; 43°17’32.3’’N 5°21’50.5’’E / Tunnel sous le Boulevard Charles Livon. https://archive.ph/reaqI. La première publication a notamment été likée par 500 pages Facebook dès le jour de sa création, dont un nombre important de pages ne comportant aucun abonné et se définissant comme des pages de beauté. https://archive.ph/EY3HM, https://archive.ph/lBHmA, https://archive.ph/IpLOd.

<!-- page 9 -->
2.1.2. delogupourpalestine.com Le nom de domaine delogupourpalestine.com 12, déposé le 19 février 2026, a utilisé le service d’anonymisation Cloudflare, et emploie le même template Wordpress que le site blogdesophie.com, visible par exemple en accédant au profil de l’auteur du site 13 (cf. ci-dessous). Les investigations de VIGINUM ont permis d’identifier une page Facebook nommée « Qui est Sébastien Delogu ? », également créée le 9 février 2026, participant à la diffusion de contenus ciblant le député. À la différence d’autres pages du dispositif, celle-ci présente des erreurs dans ses éléments de description. En effet, l’adresse courriel, les comptes Instagram et Telegram prétendument affiliés à cette page, ainsi que le username dans l’URL Facebook, orthographient mal le nom de famille du candidat alors écrit « Delgou » 14.

> **[Figure F4 - p. 9] Capture de sites WordPress (comparaison de gabarits)**
>
> Deux captures côte à côte des pages de profil d'auteur WordPress de deux sites du dispositif.
> - À gauche (blogdesophie.com) : titre « My Blog », sous-titre « My WordPress Blog », « Author: dhlkm-admin », article « Hello world! » avec le texte par défaut « Welcome to WordPress. This is your first post. Edit or delete it, then start writing! ».
> - À droite (delogupourpalestine.com) : titre « De Logu pour Palestine », « Author: kvvel », article « Hello world! » avec le même texte par défaut.
>
> Ce que la figure enseigne : les deux sites partagent le même gabarit WordPress et le contenu par défaut non personnalisé, avec des identifiants d'administrateur de type aléatoire (dhlkm-admin, kvvel) ; c'est l'un des éléments techniques communs qui relient les quatre sites à un même opérateur (le rapport cite aussi le thème « hello-elementor 3.4.6. », voir figure 5.2).

> **[Figure F5 - p. 9] Capture de page Facebook**
>
> Page Facebook « Qui est Sébastien Delogu ? » : « 1.1K followers • 0 following ». Bandeau dans un style de dossier à scandale : « QUI ÊTES-VOUS, SÉBASTIEN DELOGU ? », « Mystères, Révélations. Comptes à rendre ? », avec des mentions « DOSSIER », « SCANDALE ? » et « ZONE D'OMBRE », une loupe et des points d'interrogation. Photo de profil : caricature d'un homme en costume.
>
> Ce que la figure enseigne : la page, créée le 9 février 2026, utilise une esthétique de dénonciation sensationnaliste ; le rapport signale qu'à la différence des autres pages, elle contient des erreurs de description (adresse courriel, comptes Instagram et Telegram et nom d'utilisateur Facebook écrivent « Delgou »), indice d'une production hâtive ou automatisée.

À gauche le site blogdesophie.com ; à droite le site delogupourpalestine.com

Ci-contre, la page Facebook « Qui est Sébastien Delogu ? »

2.1.3. piquemalzero.com Le nom de domaine piquemalzero.com 15, déposé le 11 février 2026, cible le candidat LFI à la mairie de Toulouse et député de la quatrième circonscription de Haute-Garonne, François PIQUEMAL. Le site est hébergé par l’entreprise Namecheap et utilise lui aussi le service d’anonymisation Cloudflare. Édité sous WordPress, le site utilise le même template « hello-elementor 3.4.6. » que les trois autres sites détectés par VIGINUM : Capture d’écran de la page Facebook « Je suis François Piquemal blogdesophie.com, lalternative2026.com et et je ne suis rien. »

delogupourpalestine.com.

VIGINUM considère que ce site est lié à la page Facebook « Je suis François Piquemal et je ne suis rien. » 16, sur laquelle a été diffusé le hashtag #piquemalcestmal. Également créée le 9 février 2026, cette page diffuse de nombreux contenus ciblant le candidat LFI. Le 24 février 2026, la page a notamment partagé un lien vers le site piquemalzero.com, accompagné du message « Nous ne sommes pas seuls. Un nouveau site web ose poser les questions que peu osent poser. Il semble que beaucoup partagent les mêmes préoccupations. »

> **[Figure F6 - p. 9] Capture de page Facebook**
>
> Page Facebook « Je suis François Piquemal et je ne suis rien. » : « 1K followers • 0 following ». Image de couverture : un blason rouge à fleurs de lys, croix occitane et animal héraldique, qui semble être celui de la ville de Toulouse [identification probable, non précisée par le rapport], et le slogan « Toujours Indigné, Jamais Utile ». Photo de profil : caricature d'un homme souriant.
>
> Ce que la figure enseigne : page parodique à charge contre François PIQUEMAL (candidat à Toulouse), liée au site piquemalzero.com et diffusant le hashtag #piquemalcestmal ; elle appartient aux trois pages Facebook principales du dispositif, amplifiées par les mêmes comptes inauthentiques (cf. figure du graphique p. 12).

https://archive.ph/O1CBf. https://blogdesophie[.]com/author/dhlkm-admin/ et https://delogupourpalestine[.]com/author/kvvel/. https://archive.ph/GBCCG ; quiestdelgou@gmail[.]com ; instagram.com/qui.est.delgou, t.me/QueEstDelgou ; username Facebook : facebook.com/QuiEstSebastienDelgou. https://archive.ph/2TnzW. https://archive.ph/1rham.

<!-- page 10 -->
Par ailleurs, le hashtag #piquemalcestmal a également été relayé sur d’autres plateformes, dont TikTok, par le compte @drole_230 17 ou sur Instagram, via le compte équivalent de ce dernier, @elections_municipales_2026 18.

Enfin, VIGINUM a également détecté la présence d’au moins 21 comptes Facebook inauthentiques partageant tous un lien vers le site piquemalzero.com dans les commentaires d’une publication de la page officielle de François PIQUEMAL 19. Ces comptes ont tous posté exactement le même lien sous la forme « ********piquemalzero.com » entre 00h01 et 00h02 le 6 mars 2026.

> **[Figure F7 - p. 10] Capture de commentaires Facebook (amplification coordonnée)**
>
> Capture d'un Reel de la page Facebook officielle de François PIQUEMAL (un homme debout devant une fresque « UN TOIT C'EST UN DROIT - DAL31 », sous-titre « ou au DAL que je les ai vécus ») avec ses compteurs (environ 979 mentions j'aime et 320 commentaires) et, dans le panneau de droite, la liste des commentaires. Une quinzaine de commentaires sont visibles ; tous sont publiés par des comptes différents, dont les noms sont en caractères thaïs ou vietnamiens (par exemple « Huỳnh Quỳnh Vinh », « Loan Nga », « Thảo Quyên Lê », « Huu Phuoc ») ainsi que « Rafiere Nickles » et « Kala Debow », et tous contiennent exactement le même texte masqué « ********piquemalzero.com/ », avec l'indication « 1 j ».
>
> Ce que la figure enseigne : technique de commentaire en rafale (spam de lien) sous une publication légitime pour pousser le site piquemalzero.com ; le rapport compte au moins 21 comptes inauthentiques ayant posté le même lien entre 00h01 et 00h02 le 6 mars 2026, signature d'une automatisation et d'une coordination.

Commentaires déposés sur la page Facebook officielle de François PIQUEMAL.

2.1.4. Usurpation de l’identité de l’association « Enfance et Partage » sur Facebook La page Facebook @EnfanceetPartageorg, créée en février 2026 20, a usurpé l’identité de l’association française créée en 1977 « Enfance et Partage », spécialisée dans la protection de l’enfance. Si l’association possède bien une page Facebook depuis 2012 21 comptabilisant plus de 10 000 abonnés, la page usurpant son identité est quant à elle suivie par 1 900 comptes aux caractéristiques inauthentiques.

> **[Figure F8 - p. 10] Capture de pages Facebook (usurpation d'identité)**
>
> Comparaison de deux pages Facebook « Enfance et Partage ».
> - À gauche, la page @EnfanceetPartageorg (usurpation) : « 1,9 K followers • 0 suivi(e)s », description « Association reconnue d'utilité publique, Enfance & Partage forme, sensibilise et accompagne enfants, familles et professionnels pour protéger les enfants et prévenir toutes les formes de violences depuis près de 50 ans. », catégorie « Organisation à but non lucratif », logo « e&P enfance & partage - L'enfance est fragile, Protégeons-la. ».
> - À droite, la page authentique @EnfanceetPartage : « 10 K followers • 146 suivi(e)s », description « Depuis près de 50 ans, Enfance & Partage agit pour protéger, soutenir et défendre les enfants victimes de toutes formes de violence. Nous menons des actions de prévention, d'écoute et d'accompagnement auprès des victimes. », même catégorie.
> Les deux pages reprennent la même bannière : « Si vous pensez qu'un enfant est en danger : CONTACTEZ LE 119 » et les mêmes boutons « En savoir plus », « Suivre », « Rechercher ».
>
> Ce que la figure enseigne : usurpation d'identité visuelle quasi parfaite (logo, bannière, nom, catégorie) d'une association de protection de l'enfance, pour donner de la crédibilité aux accusations de pédophilie visant François PIQUEMAL. L'indicateur d'inauthenticité est l'écart d'audience : 1,9 K abonnés inauthentiques, aucun abonnement, contre 10 K et 146 pour la vraie page. Le rapport rattache cette page au MOI avec un niveau de confiance élevé.

Captures d’écran de la page Facebook @EnfanceetPartageorg (à gauche) usurpant l’identité de la page @EnfanceetPartage (à droite) de l’association française loi 1901 « Enfance et Partage ».

https://archive.ph/H256b. Ce compte a été suspendu ou supprimé au cours de la journée du 5 mars 2026. https://facebook[.]com/reel/2423327021466972. https://archive.ph/BPnwJ. La page est rattachée à une adresse mail Proton : enfanceetpartageorg@proton[.]me. @EnfanceEtPartage https://archive.ph/SWLkX.

<!-- page 11 -->
Entre le 15 et 22 février 2026, les premières publications de la fausse page sont des copies identiques des publications du vrai compte de l’association 22. Néanmoins, à partir du 22 février 2026, des publications originales ont été diffusées, dénonçant une forme d’immunité dont bénéficieraient les hauts représentants politiques et religieux coupables d’agressions sur mineurs. Puis, du 25 au 27 février 2026, VIGINUM a identifié le partage de trois publications relayant de manière trompeuse une prétendue enquête de justice à l’encontre de François PIQUEMAL pour agression sur mineur, lorsqu’il était enseignant au lycée du Mirail à Toulouse 23. Ces publications n’ont comptabilisé aucune réaction, à l’instar des autres contenus partagés sur la page jusqu’au 3 mars 2026, date de la dernière activité observée. Par ailleurs, VIGINUM n’a observé aucun relai de ces accusations trompeuses envers François PIQUEMAL, ni sur Facebook, ni sur d’autres plateformes, malgré un potentiel pré-positionnement sur X 24.

Outre la date de création et le narratif relayé par cette page, VIGINUM a également observé que @EnfanceetPartageorg avait intégré plusieurs groupes Facebook (voir annexe 5.3) dans la même temporalité que plusieurs comptes inauthentiques du MOI. VIGINUM rattache donc avec un niveau de confiance élevé la page @EnfanceetPartageorg au MOI Rokh Solis.

2.1.5. « La vérité cachée sur David Guiraud » Enfin, les investigations conduites par VIGINUM ont également permis de détecter la présence sur Facebook de deux pages ciblant le candidat LFI à la mairie de Roubaix et député de la huitième circonscription du Nord, David GUIRAUD.

Captures d’écran des pages Facebook ciblant David GUIRAUD

> **[Figure F9 - p. 11] Captures de pages Facebook**
>
> Deux pages Facebook ciblant David GUIRAUD, avec la même bannière : un mur de briques couvert d'affiches arrachées portant en graffiti noir et rouge « La vérité cachée sur David Guiraud ».
> - À gauche : « Guiraud Est Zéro. Les faits que David Guiraud ne veutpas que voussachiez » (sic), « 752 followers • 0 following », photo de profil vide.
> - À droite : « Toute la vérité sur David Guiraud », « 928 followers • 0 following », photo de profil en dessin : un homme barbu allongé dans un lit à côté d'une chèvre.
>
> Ce que la figure enseigne : deux pages jumelles (bannières et photos de profil identiques selon le rapport) ; l'une est une page créée en 2022 puis renommée a posteriori, l'autre a été créée le 13 février 2026. Même procédé que pour François PIQUEMAL (pages nommées sur un schéma « …Zéro / …ne sait rien »), et même réseau de comptes amplificateurs inauthentiques, d'où le rattachement au MOI Rokh Solis avec un niveau de confiance élevé.

Une première page créée en 2022 a été renommée a posteriori « Toute la vérité sur David Guiraud», tandis que la seconde page a été créée le 13 février 2026 et se nomme « Guiraud Est Zéro. Les faits que David Guiraud ne veutpas que voussachiez 25 ». Selon le même procédé que celui utilisé pour cibler François PIQUEMAL, ces deux pages sont destinées à la publication de contenus reprochant notamment à David GUIRAUD ses positions propalestiniennes.

Plusieurs caractéristiques techniques permettent de rattacher ces deux pages au MOI Rokh Solis avec un niveau de confiance élevé. D’une part, les deux pages présentent des photos de profils et de bannières identiques entre elles. D’autre part, l’origine des interactions sur les différentes publications observées ont montré l’implication du même réseau de comptes inauthentiques que celui utilisé pour les autres pages Facebook précitées (cf. partie 2.1.6). De plus, l’une des deux pages indique une adresse

La fausse page renvoie également au vrai site, aux vrais comptes de réseaux sociaux et au vrai numéro de téléphone de l’association. https://archive.ph/L5k75 ; https://archive.ph/dNZyP ; https://archive.ph/Gzi0g. Le compte X @EnfanceP16039 (archive.ph/wip/7fiqj) créé en mars 2026 affiche une photo de profil usurpant le logo de l’association avec comme username « Enfance & Partage ». Le compte n’affiche aucune publication, aucun abonnement et aucun abonné. https://archive.ph/kl2VJ et https://archive.ph/gx2bb.

<!-- page 12 -->
courriel, info@guiraudestzero[.]com, reprenant les expressions utilisées contre François PIQUEMAL. VIGINUM n’a pas été en mesure de détecter l’existence d’un site relié à cette adresse courriel.

2.1.6. Relais inauthentiques sur Facebook potentiellement impliqués dans d’autres opérations d’ingérences numériques étrangères L’analyse présentée ci-dessous ne se concentre que sur les trois principales pages identifiées par VIGINUM (« Le Blog de Sophie », « Qui est Sébastien Delogu ? » et « Je suis François Piquemal et je ne suis rien »). En effet, bien qu’il ait été possible de détecter des relais communs sur les pages ciblant David GUIRAUD, la publication d’un article le 9 mars 2026 par le journal Le Monde 26 dévoilant une partie de la campagne conduite au moyen du MOI Rokh Solis, a provoqué la suppression rapide de la plupart des avatars au centre de ce MOI. De surcroît, plusieurs éléments techniques identifiés à partir de comptes inauthentiques sur Facebook semblent indiquer la présence de comptes équivalents, désormais supprimés ou suspendus, sur d’autres plateformes comme Instagram, X ou TikTok, eux aussi probablement destinés à amplifier les actifs numériques du MOI Rokh Solis. Ainsi, autour des trois pages Facebook qui accompagnent les trois premiers sites mentionnés, VIGINUM a identifié la mobilisation artificielle de nombreux comptes, dont le rôle se résume notamment à la production de commentaires sous les publications diffusées sur les pages Facebook du dispositif. Par exemple, sur la page « Qui est Sébastien Delogu ? », la vidéo publiée le 16 février 2026 comportait 18 commentaires encore visibles au moment des investigations. Neuf d’entre eux ont été postés le 21 février 2026 à exactement 12h40 par des comptes présentant des caractéristiques similaires (notamment des profils masculins créés ou modifiés en février 2026, localisés la plupart du temps aux États-Unis). La présence de ces commentaires s’étend sur les pages « Le Blog de Sophie » et « Je suis François Piquemal et je ne suis rien. ». Sur 338 commentaires au total, 138 utilisateurs uniques ont été identifiés. Parmi eux, trois comptes commentent les trois pages Facebook 27, tandis que 48 d’entre eux commentent au moins deux des trois pages du dispositif. La temporalité de la publication des commentaires sur ces trois différentes pages indique une forte mobilisation coordonnée de ces comptes au cours du mois de février 2026. Par exemple, alors que ces pages ne bénéficiaient d’aucune visibilité, plus d’une centaine de commentaires a été déposée dans un laps de temps très court sur la page « Qui Est Sébastien Delogu ? » autour du 21 février 2026.

> **[Figure F10 - p. 12] Graphique (histogramme empilé)**
>
> Histogramme « Chronologie de publication des commentaires par des comptes inauthentiques sur trois des cinq pages Facebook du MOI Rokh Solis ». Axe horizontal : date du commentaire, de la mi-février 2026 (repères « Feb 18 », « Feb 20 », « Feb 22 », « Feb 24 », « Feb 26 », « Feb 28 ») à la fin du mois. Axe vertical : nombre de commentaires par tranche de trois heures (échelle de 0 à 120). Séries (légende « Pages Facebook Rokh Solis ») : QuiEstSebastienDelgou (bleu), LeBlogdeSophie0 (rouge), piquemalrien (vert).
>
> Valeurs lues sur le graphique (approximatives) :
> - Un pic isolé d'environ 115 commentaires sur QuiEstSebastienDelgou, situé juste après le repère « Feb 20 », suivi d'une barre d'environ 21 ; des barres d'environ 8 à 9 peu avant et après (vers Feb 19 et Feb 21).
> - Un second épisode, entre « Feb 22 » et « Feb 24 » (environ le 23 février), de quatre tranches successives : environ 54 (bleu), 55 au total (environ 24 bleu et 31 vert), 45 au total (environ 10 bleu et 35 vert), puis 18 au total (environ 9 bleu et 9 vert) ; une barre bleue d'environ 26 vers le 24 février.
> - De petites barres rouges (LeBlogdeSophie0, 2 à 7 commentaires) autour du 21, du 26 et du 28 février et au début mars.
> Le rapport situe la mobilisation « autour du 21 février 2026 » ; sur l'axe le pic apparaît juste après le repère Feb 20, ce que l'annotateur ne peut pas réconcilier avec certitude (fuseau horaire ou alignement de l'axe).
>
> Ce que la figure enseigne : les commentaires inauthentiques arrivent en salves très courtes, sur des pages qui n'avaient aucune visibilité organique, et se concentrent sur la page « Qui est Sébastien Delogu ? » puis sur celle de Piquemal. Le rapport chiffre l'ensemble : 338 commentaires, 138 utilisateurs uniques, 3 comptes présents sur les trois pages et 48 sur au moins deux.

https://www.lemonde.fr/politique/article/2026/03/09/sebastien-delogu-francois-piquemal-et-lfi-cibles-par-une-campagne-de- desinformation-en-pleine-campagne-des-municipales_6670119_823448.html. « Valéry Trottier » (https://archive.ph/4Rfs9), « Gervais Barnier » (https://www.facebook.com/profile.php?id=61576708183408 ), « Remy Picard » (https://www.facebook.com/profile.php?id=61570856034692 ).

<!-- page 13 -->
Au sein de ce cluster de 138 comptes, cinq d’entre eux 28 ont également repartagé des publications issues des pages « Je suis François Piquemal et je ne suis rien » et « Qui est Sébastien Delogu ? ». Ces comptes- relais présentent les mêmes caractéristiques que celles décrites ci-dessus :

- création les 11 et 12 février 2026 ;

- incarnation d’un individu masculin, comme « Kendrick Castro » et « Hanley Dolton » ;

- localisation dans une ville américaine, comme Chicago.

Toujours sur Facebook, VIGINUM a également été en mesure d’identifier un autre groupe de comptes inauthentiques a priori destiné au partage des contenus ciblant les différents candidats LFI vers des groupes Facebook. Au total, a minima 39 comptes ont participé à ce schéma de diffusion, dont au moins 20 ont été suspendus ou supprimés (cf. annexe 5.3). Les comptes encore actifs présentent des caractéristiques inauthentiques communes :

- photos de profil générées par IA ;

- désignation par des noms francophones très répandus (par exemple : Claire Martin, Elise Martin, Etienne Martin, Sophie Dupont, Luc Dupont, etc.) ;

- fils de publications ou republications identiques pour certains comptes (par exemple, « Lea Leclerc » et « Camila Savard » ont republié les mêmes contenus : des vidéos datant de décembre 2025 depuis la page « ELNET France » 29, et des publications émises par les pages « CNEF 30 » ou encore « Christ Apostolic Church France ») ;

- existence de comptes équivalents sur Instagram et/ou sur X.

Comptes inauthentiques amplifiant les contenus du MOI Rokh Solis sur Facebook.

> **[Figure F11 - p. 13] Captures de profils Facebook (comptes inauthentiques)**
>
> Quatre profils Facebook (thème sombre) de comptes relais.
> - Lea Leclerc : « 1,3 K ami(e)s » ; intro « Bordeaux - Jeux • Musique • Rêves » ; habite à Bordeaux, de Reims, membre depuis mars 2018 ; Instagram « lea_6leclerc ».
> - Julie Simon : « 2,9 K ami(e)s » ; intro « Architectural Designer - Storyteller through spaces & visuals » ; travaille chez « Architectural designer », a étudié à « University Heights », habite à Manhattan ; Instagram « julie_12simon ».
> - Camila Savard : « 36 ami(e)s » ; intro « Danser, c'est ma façon de vivre! » ; travaille chez « Fitness Coach », habite et est de Marseille, membre depuis août 2022 ; Instagram « fitness.camila ».
> - Théo Blanc : « 4,3 K followers • 1,2 K suivi(e)s » ; intro « Networks and code, with a touch of drama » ; profil « Service d'ingénierie », travaille chez « Electrical Engineering Technician », a étudié à « University Gardens, New York », habite à Syracuse (New York), en couple ; Instagram « the0blanc ».
> Le texte du rapport indique que les comptes encore actifs de ce cluster présentent des photos de profil générées par IA (la légende ne qualifie pas chaque portrait).
>
> Ce que la figure enseigne : spécimens du cluster d'au moins 39 comptes Facebook qui partagent les contenus anti-LFI vers des groupes : noms francophones très répandus, localisations françaises ou américaines, et, observation de l'annotateur non signalée par le rapport, des champs « employeur » remplis avec un intitulé de poste (« Fitness Coach », « Architectural designer », « Electrical Engineering Technician »), signe de profils fabriqués. Chaque compte a un équivalent Instagram et/ou X.

https://www.facebook.com/profile.php?id=61587747128345 (Hanley Dolton), https://www.facebook.com/profile.php?id=6158 7584237367 (Kendrick Castro), https://www.facebook.com/profile.php?id=61587664271544 (Simon Atkinson), https://www.face book.com/profile.php?id=61587768221403 (Staphan Nicholls), https://www.facebook.com/profile.php?id=61588190360349 (Dom Tomlinson). https://archive.ph/BvZcd. Page Facebook officielle du « Conseil National des Évangéliques de France ».

<!-- page 14 -->
La plupart des groupes au sein desquels se sont positionnés ces comptes inauthentiques ciblent des audiences francophones sur des thématiques concernant la politique française, qu’elle soit locale ou nationale, et dont la ligne éditoriale est souvent contestataire ou satirique. Par exemple :

- 23 des 39 comptes inauthentiques ont intégré le groupe « Militant de Droite » le 7 septembre 2025 31 ;

- 20 des 39 comptes inauthentiques ont intégré le groupe « @Combat Républicain 2027 » le 7 septembre 2025 32 ;

- 20 des 39 comptes inauthentiques ont intégré le groupe « Le Canard Enchaîné Libre » le 7 septembre 2025 33 ;

- 18 des 39 comptes inauthentiques ont intégré le groupe « info citoyenne pour demain » le 7 septembre 2025 34.

Comme indiqué ci-dessus, la plupart des groupes ciblés par ces comptes inauthentiques ont été rejoints à l’automne 2025. Cependant, quelques groupes ont été intégrés par ces mêmes comptes en janvier et février 2026 (cf. annexe 5.1). Par exemple, le 23 février 2026, ces comptes ont intégré les groupes « Arabes à Toulouse DZ » et « Les Musulmans de Marseille », semblant indiquer que les opérateurs du MOI Rokh Solis ont convoité des audiences locales au cours du déploiement de la campagne informationnelle contre François PIQUEMAL et Sébastien DELOGU.

Plusieurs éléments permettent également d’affirmer que certains de ces comptes ont pu être utilisés dans le cadre d’anciennes campagnes, tandis que d’autres ont déjà changé de nom pour éventuellement servir à de futures opérations informationnelles.

Par exemple, le compte « Maxime Meireles », ayant intégré six groupes différents le 7 septembre 2025, se nomme désormais « Romi Shushan 35 » et indique une localisation à Tel Aviv-Jaffa. C’est aussi le cas pour le compte « Shirel Matana 36 », anciennement « Amélie Charpentier », qui a été renommé en mars 2026, et se localise à Rishon Letsion en Israël.

Au sein de ce cluster, VIGINUM a également détecté l’existence d’un autre compte, anciennement nommé « Manon Berger 37 », et ayant rejoint entre le 7 et le À gauche, le compte anciennement nommé « Maxime Meireles » ; à 8 septembre 2025 cinq des groupes droite, le compte anciennement appelé « Amélie Charpentier »

> **[Figure F12 - p. 14] Captures de profils Facebook renommés**
>
> Deux profils Facebook (thème sombre) de comptes renommés.
> - À gauche, « Romi Shushan » (anciennement « Maxime Meireles ») : « 2,6 K ami(e)s » ; photo de profil en noir et blanc d'une femme à chapeau prenant un selfie dans un miroir ; couverture : fauteuils vert sarcelle devant une table de verre ; intro : une phrase en hébreu [texte hébreu partiellement lisible, avec un émoji] ; « Habite à Tel Aviv-Jaffa » ; « Célibataire ».
> - À droite, « Shirel Matana » (anciennement « Amélie Charpentier ») : « 1,1 K ami(e)s » ; photo de profil d'une femme devant une fresque colorée ; couverture : vue nocturne de remparts d'une vieille ville ; intro composée d'émojis ; « Habite à Rishon Letsion ».
>
> Ce que la figure enseigne : des comptes francophones du cluster (intégrés aux groupes le 7 septembre 2025) ont été renommés avec des identités et des localisations israéliennes (Tel Aviv-Jaffa, Rishon Letsion, renommage en mars 2026 pour Shirel Matana), ce que le rapport lit comme un recyclage de comptes entre opérations. Ces deux comptes réagissent ensuite aux publications du cluster angolais (figures suivantes).

identifiés 38. Ce dernier se présente aujourd’hui sous le nom de « Forsane Alizza » (cf. partie 2.2.3).

https://www.facebook.com/groups/1499298420144744/permalink/31282752168039309/ - https://archive.ph/F6omb. https://www.facebook.com/groups/253970028392550/permalink/2363956307393901/ - https://archive.ph/kWMBO. https://www.facebook.com/groups/canardlibre/permalink/31432631416352831/ - https://archive.ph/1K5sA. https://www.facebook.com/groups/179555479599180/posts/1803635097191202/ - https://archive.ph/pL4bD. https://archive.ph/q059Z. https://archive.ph/5BYtq. https://www.facebook.com/forsanealizza/. Voir les groupes « info citoyenne pour demain », « Le Canard Enchaîné Libre », « @Combat Républicain 2027 », « GAUCHE NON POUTINISTE NON PRO HAMAS » et « Militants de Droite ».

<!-- page 15 -->
De plus, parmi les 39 comptes inauthentiques décrits ici, certains d’entre eux semblent avoir été utilisés pour des campagnes informationnelles plus anciennes ciblant les États-Unis, notamment les élections municipales de New York en novembre 2025. En effet, VIGINUM a identifié l’intégration de plusieurs comptes à des groupes anglophones :

- quatre comptes ont intégré le groupe « #WalkAway Campaign » en octobre 2025 39 ;

- quatre comptes ont intégré le groupe « This is Woodhaven, NYC » en octobre 2025 40 ;

- six comptes ont intégré le groupe « Brockton Political Central » en octobre 2025 41.

Probablement dans le cadre de cette dynamique, les comptes « Julie Simon » et « Théo Blanc » ont relayé 42 les 6 et 7 octobre 2025, des publications issues de la page Facebook d’Andrew CUOMO, candidat démocrate aux élections municipales de New York.

De surcroît, les investigations conduites par VIGINUM ont permis d’identifier, en lien avec les comptes précités, un autre cluster de comptes présentant des caractéristiques identiques mais ciblant cette fois des audiences en Angola. En effet, 48 comptes encore actifs à ce jour ont été répertoriés et présentent plusieurs éléments similaires au cluster décrit précédemment (cf. annexe 5.3) :

- photos générées par intelligence artificielle ;

- utilisation de noms à consonnance lusophone (« Maria Sousa », « Paulo Carvalho », « Luisa Perreira » etc.) ;

- republications communes entre ces différents comptes et coordination de leurs interactions auprès des mêmes publications via des likes ou des commentaires.

Exemples de comptes issus du cluster ciblant l’Angola sur Facebook.

> **[Figure F13 - p. 15] Captures de profils Facebook (cluster Angola)**
>
> Quatre profils Facebook (thème sombre) du cluster de 48 comptes ciblant l'Angola.
> - José Silva : « 114 ami(e)s » ; intro en portugais « Trabalho na construção civil - gosto de ver projetos tomarem forma » ; travaille chez « Emt - Empresa De Mão De Obra Terceirizada Ltda » ; a étudié à « Escola de Formação Técnica Profissional de Saúde do Lobito(EFTPS) » ; habite à Benguela, Benguela, Angola ; en couple ; photo d'un homme casqué en gilet orange.
> - Ana Costa : « 736 followers • 22 suivi(e)s » ; intro « Dedicada aos detalhes na loja e à família » ; « Profil · Shopping et vente au détail » ; gérante de « Lanchonete da Cidade, à Moda Luanda » ; habite à Benguela ; de Huambo ; Instagram « ana.costa710 ».
> - Nancy Kato : « 668 followers • 8 suivi(e)s » ; intro « Vendo roupas em Benguela, estilo e moda é minha paixão » ; « Profil · Création digitale » ; travaille chez « vendedora de roupas » ; a étudié à « Colegio Os Brilhantes » ; habite à Benguela ; « En union civile » ; membre depuis octobre 2024.
> - Soykat Hasan : « 33 ami(e)s » ; intro « Encontre-me nas montanhas ou explorando histórias escondidas » ; travaille chez « Tourist Guide » ; a étudié à « soyod ahmed university & colleg,gabtoli,bogra » ; habite à Gabela (Angola) ; photos de randonnée.
>
> Ce que la figure enseigne : second cluster (noms lusophones comme « Maria Sousa », « Paulo Carvalho », « Luisa Perreira ») construit selon le même modèle que le cluster français : photos générées par IA, identités banales et localisées, republications et interactions communes. On y relève des incohérences de fabrication (nom « Soykat Hasan » et université du Bangladesh pour un guide à Gabela, Angola) ; le rapport y voit le signe d'un même opérateur pour la France et l'Angola.

Le comportement de ces comptes repose sur l’amplification coordonnée de contenus produits par le gouvernement angolais 43 ou de contenus faisant la promotion du Movimento Popular de Libertação de

https://www.facebook.com/groups/realwalkaway/permalink/4239894526245376/ - https://archive.ph/ZVi7d ; la campagne #WalkAway semble avoir été initiée et soutenue en ligne à partir de 2018 par des membres de la sphère MAGA aux États-Unis pour inciter les citoyens américains renoncer au parti démocrate. https://www.facebook.com/groups/thisisWoodhavenNYC/posts/1634959334142897/ - https://archive.ph/jzhvO ; On y retrouve les comptes de « Luc Dupont », « Elise Martin », « Julien Garcia » et « Amélie Charpentier ». https://www.facebook.com/groups/1995939550643638/posts/4245488089022095/ - https://archive.ph/6bhbn ; voir les comptes « Thomas Durand » ; « Luc Dupont », « Claire Bernard », « Julien Garcia », « Amélie Charpentier » et « Elise Martin ». https://archive.ph/Y3ZAb ; https://archive.ph/Xk9tV ; https://archive.ph/z83yE. https://archive.ph /dGVzL.

<!-- page 16 -->
Angola 44 (Mouvement Populaire de Libération de l’Angola, MPLA). En effet, au même titre que leurs homologues du cluster ciblant les élections municipales en France, ces comptes se sont également coordonnés pour se positionner dans plusieurs groupes Facebook ciblant différentes audiences en Angola, notamment des groupes en faveur du MPLA (cf. annexe 5.3). Enfin, VIGINUM a identifié plusieurs éléments témoignant d’une certaine porosité entre les comptes francophones impliqués dans les opérations ayant ciblé les élections municipales et les comptes lusophones ciblant une audience angolaise :

- à quatre reprises, des interactions ont été observées entre ce groupe de comptes dits « angolais » et celui ciblant la France. En effet, « Diogo Pereira », « Miguel Correia », « Luzia Andrade » et « Paulo Nunes » ont bénéficié de likes sur certaines de leurs publications de la part des comptes « Romi Shushan » et « Shirel Matana ». Les autres réactions sur cette publication laissent également apparaître l’existence d’un autre groupe de comptes incarnant des individus d’origine ukrainienne 45 ;

- le compte « Kodjo Mensah » s’était auparavant nommé « Ana Dos Santos », et pourrait donc éventuellement être l’équivalent du compte X @its_anasantos (cf. partie 3.1 et annexe 5.4) ;

- le compte « Carlos Fernandes » était auparavant nommé « Alex Dupuis » ;

- plusieurs commentaires déposés par ces comptes sous des communications officielles du gouvernement angolais ont été déposés en français et non en portugais (attestant d’une potentielle erreur de traduction) 46.

Réactions de « Romi Shushan » et « Shirel Matana » aux republications du cluster ciblant l’Angola sur

> **[Figure F14 - p. 16] Captures de listes de réactions Facebook (montage de quatre panneaux)**
>
> Montage de quatre captures sombres de publications de comptes du cluster angolais, chacune avec la fenêtre « Toutes » des réactions ouverte. Le contenu republié est un article sportif intitulé « Futebol e Orgulho Nacional: … em a Argentina Para Angola » (rubrique « DESPORTO », site [partiellement lisible, finissant en « …ticias.ao »]).
> - Haut gauche, profil « Luzia Andrade » (habite à Benguela, Huambo, Angola) : 3 réactions « j'aime » et 1 « cœur » ; réacteurs : Oksana Ivanova, Liliia Bondarenko, Tetiana Moroz, Romi Shushan (cœur).
> - Haut droite, profil « Diogo Pereira » : 5 « j'aime » et 1 « cœur » ; réacteurs : Viktor Petrenko, Serhiy Koval, Oksana Ivanova, Liliia Bondarenko, Tetiana Moroz, Romi Shushan.
> - Bas gauche, profil « Paulo Nunes » : 4 « cœur » et 2 « j'aime » ; réacteurs : Shirel Matana, Viktor Petrenko, Serhiy Koval, Liliia Bondarenko, Tetiana Moroz, Romi Shushan.
> - Bas droite, profil « Miguel Correia » : 2 « cœur », 1 « j'aime », 1 « wow » (4 réactions affichées, 3 noms visibles) ; réacteurs visibles : Oksana Ivanova, Tetiana Moroz, Romi Shushan.
>
> Ce que la figure enseigne : les mêmes comptes (« Romi Shushan » et « Shirel Matana », issus du cluster français, ainsi qu'un petit groupe de comptes incarnant des individus d'origine ukrainienne : Oksana Ivanova, Liliia Bondarenko, Tetiana Moroz, Serhiy Koval, Viktor Petrenko) réagissent aux publications des comptes angolais : il y a donc de la porosité entre le cluster qui a ciblé les municipales françaises et le cluster qui amplifie le gouvernement angolais et le MPLA, ce qui soutient l'hypothèse d'un opérateur unique.

Le MPLA a été fondé en 1956 et a participé à la guerre d’indépendance de l’Angola entre 1961 et 1975. Aujourd’hui, João LOURENÇO, président de la république d’Angola, est également le chef du MPLA. À l’image de « Teitiana Moroz », « Serkhiy Koval » , « Oksana Ivanova » et Lilia Bondarenko (ID: 61579822949833). Cf. archives : https://archive.ph/Xumnj, https://archive.ph/colBn et https://archive.ph/S0Zgb. https://www.facebook[.]com/photo/?fbid=1195085675982954 ; https://www.facebook[.]com/photo/?fbid=1193461262812062 ; https://www.facebook[.]com/photo/?fbid=1192109809613874.

<!-- page 17 -->
Ainsi, VIGINUM a pu établir un lien entre des comptes actifs lors d’opérations ayant ciblé les élections municipales et des comptes impliqués dans l’amplification de publications gouvernementales angolaises. Or, des éléments techniques établis par le Service (partie 3.3.) semblent confirmer l’hypothèse selon laquelle un seul et même opérateur pourrait être à l’origine des deux opérations informationnelles.

2.2. Mise en scène de l’opposition entre un prétendu écosystème islamiste radical et une soi-disant association souverainiste Dans le cadre des investigations menées sur la campagne informationnelle ayant ciblé des candidats LFI, VIGINUM a également détecté le déploiement simultané d’actifs numériques destinés à s’opposer autour de narratifs instrumentalisant la communauté musulmane en France. Les différents avatars et sites déployés lors de cette campagne visent en effet à simuler le clivage entre un prétendu segment séparatiste islamiste, et un pseudo mouvement identitaire et conservateur.

2.2.1. lalternative2026.com Le nom de domaine lalternative2026.com, a été enregistré le 9 février 2026, a son adresse IP réelle anonymisée avec Cloudfare, et est hébergé chez Namecheap. Le site se présente comme la vitrine de « L’Alternative 2026 », une prétendue « initiative citoyenne lancée en amont des élections municipales de mars 2026 », dont l’objectif serait « d’aider les citoyens Capture d’écran du site lalternative2026.com musulmans à identifier, dans chaque ville, le candidat qui représentera le mieux ses intérêts » 47.

> **[Figure F15 - p. 17] Capture de site**
>
> Page d'accueil de lalternative2026.com. En-tête bleu nuit avec logo « L'Alternative » (A stylisé avec un croissant vert), « Élections Municipales 2026 - Pour une France plus Musulmane » et deux boutons « Liste » et « Carte ». Titre : « Défendre les intérêts musulmans en France » ; sous-titre : « Aider les citoyens musulmans à identifier dans chaque ville le candidat qui représente le mieux les intérêts, les valeurs et les besoins de leur communauté. » Visuel : une femme voilée devant un drapeau français (image générée avec Gemini d'après le rapport). Sept revendications affichées avec pictogrammes :
> - Autonomie communautaire ;
> - Réduction du contrôle de l'État dans les structures éducatives distinctes ;
> - Autorisation du port de signes religieux ;
> - Construction de mosquées et de minarets ;
> - Autorisation et extension de l'appel à la prière ;
> - Légalisation de structures séparées par genre (piscines, centres sportifs, hôpitaux) ;
> - Instauration de systèmes juridiques religieux distincts (notamment l'application de la charia).
>
> Ce que la figure enseigne : le site est un leurre de projet communautariste, enregistré le 9 février 2026, qui cartographie 108 candidats de gauche (principalement LFI) comme favorables à ce programme ; l'accumulation de revendications maximalistes sert à associer LFI à un séparatisme musulman fictif. Les illustrations et le logo générés par IA, comme sur blogdesophie.com, sont un indice de rattachement au même opérateur.

À ce titre, le site cartographie 108 candidats de gauche, principalement membres de La France Insoumise, ou dont la liste est soutenue par le parti. Le site présente ces candidats comme étant en mesure de faciliter l’intégration de la « vie musulmane » en France s’ils venaient à être élus. Les illustrations du site et son logo ont été générés à l’aide du logiciel d’intelligence artificielle générative Gemini 48, tout comme certaines des illustrations présentes sur le site blogdesophie.com. En février 2026, dans le cadre d’une stratégie d’optimisation du référencement en ligne de lalternative2026.com, les opérateurs du MOI Rokh Solis ont utilisé et payé un service de placement de contenus en ligne, ABNewswire.com, pour placer un article faisant la promotion du site sur différents sites légitimes tels que celui de l’agence Associated Press (apnews.com) ou le site de relations publiques OpenPr 49. VIGINUM n’a observé que cinq partages du site lalternative2026.com sur les réseaux sociaux, notamment par un compte TikTok dénommé @renaissancecatholiquefr le 24 février 2026 50, et par une page Facebook dénommée « Forsane Alizza » le 2 mars 2026 51.

https://web.archive.org/web/20260228175025/https://lalternative2026.com/. Indication présente dans le nom du fichier image : https://lalternative2026[.]com/wp-content/uploads/2026/02/cropped- Gemini_Generated_Image_kmrhexkmrhexkmrh-192x192.png. https://ghostarchive.org/archive/MVSwF et https://archive.ph/VM9cz. https://archive.ph/sxM2F. https://archive.ph/HOgbR.

<!-- page 18 -->
2.2.2. rcn-france.org, un cinquième site alertant sur l’existence de lalternative2026.com Le compte TikTok @renaissancecatholiquefr a été créé le 12 septembre 2024 52 et est utilisé pour diffuser principalement des vidéos aux narratifs anti-musulmans. Dans la description de ce compte figure le site rcn-france.com, enregistré le 26 mars 2025. Ce nom de domaine effectue une redirection vers le site rcn-france.org, qui dispose de caractéristiques techniques différentes de celles des quatre sites précités.

Créé le 25 mars 2025 et utilisant le service d’anonymisation Cloudflare, rcn-france.org se présente comme la vitrine d’un mouvement politique français à la ligne conservatrice et souverainiste, nommé « Renaissance Catholique Nationale » 53. Ce mouvement, ne possédant aucune existence préalable, semble s’inspirer du nom de « Renaissance Catholique », un authentique mouvement catholique conservateur fondé en 1988 qui vise à « diffuser les valeurs chrétiennes » 54. Le site rcn-france.org redirige vers un formulaire Jotform associé à un numéro de téléphone français 55, (sur lequel VIGINUM n’a trouvé aucune information), vers un compte Instagram aujourd’hui suspendu, @renaissance.catholique, et vers le compte TikTok précité. Les investigations menées par VIGINUM ont permis d’identifier l’organisation de deux actions dans le champ physique à Paris. Une vidéo, diffusée le 9 février 2026 sur le compte TikTok @renaissancecatholiquefr, montre un individu brandissant un drapeau avec le logo de l’organisation devant la Sorbonne, place du Panthéon. Dans cette même vidéo, un autre individu brandissant un tract avec le logo de « Renaissance Catholique Nationale » a été localisé au 141 rue de Javel 56. Ce tract comporte, de la même manière que ceux publiés par leblogedesophie.com, un code QR renvoyant pour sa part au compte TikTok @renaissancecatholiquefr.

Enfin, le 24 février 2026, @renaissancecatholiquefr a diffusé sur TikTok une vidéo mettant en garde contre un « nouveau site Internet » qui « prétend aider les électeurs à s’orienter lors des élections municipales françaises de 2026 », avec un visuel du site lalternative2026.com, constituant un lien indirect avec l’écosystème des quatre premiers sites du MOI 57.

Capture d’écran du site rcn-france.org.

> **[Figure F16 - p. 18] Capture de site**
>
> Bannière d'accueil de rcn-france.org : fond bleu texturé avec un aigle bicéphale couronné en filigrane ; en haut à gauche, un médaillon rond « RENAISSANCE CATHOLIQUE NATIONALE » avec une croix sur des bandes verticales rouge, blanc et bleu ; slogan « POUR LA FOI, LA PATRIE ET LA TRADITION » (« POUR LA FOI » en bleu, « LA PATRIE » en blanc, « ET LA TRADITION » en rouge).
>
> Ce que la figure enseigne : vitrine du pseudo-mouvement « Renaissance Catholique Nationale » (conservateur, souverainiste, identitaire), qui n'a aucune existence préalable et imite le nom d'un mouvement authentique ; c'est le pôle antagoniste que le dispositif met en scène face au faux projet musulman de lalternative2026.com. Le site redirige vers un formulaire Jotform, un compte Instagram suspendu et le compte TikTok @renaissancecatholiquefr.

Ci-contre, publication de @renaissancecatholiquefr sur TikTok montrant un tract à Paris

> **[Figure F17 - p. 18] Capture de publication TikTok**
>
> Capture d'une vidéo TikTok du compte renaissancecatholiquefr datée « 2-9 » (9 février d'après le rapport), montrant une main qui tient un tract dans une rue de Paris. Légende : « La France est un pays chrétien, elle l'a toujours été… more », option « See translation ». Compteurs affichés : 262 mentions j'aime, 0 commentaire, 344 enregistrements. Tract : logo de Renaissance Catholique Nationale, texte « CE QUI NOUS UNIT EST PLUS FORT QUE CE QUI NOUS DIVISE. UNE FOI VIVANTE. UNE FRANCE ENRACINÉE. UNE TRADITION ASSUMÉE. » (partiellement masqué par une icône), code QR, puis « SCANNEZ. ENTREZ DANS LA RENAISSANCE. ».
>
> Ce que la figure enseigne : action dans le champ physique (distribution ou collage de tracts, localisés au 141 rue de Javel à Paris) reliée au numérique par un code QR renvoyant au compte TikTok ; la charte graphique du tract est semblable à celle des tracts à QR code collés à Marseille pour blogdesophie.com (page 8), indice de coordination entre les deux écosystèmes pourtant présentés comme antagonistes.

Entre septembre 2024 et août 2025, le compte TikTok @renaissancecatholiquefr s’est mobilisé pour promouvoir le hashtag pro- israélien #TheWestIsNext et des narratifs islamophobes ciblant une audience anglophone. Il est par la suite renommé et a redirigé son action vers des narratifs souverainistes ciblant l'audience francophone. https://web.archive.org/web/20250329084635/https://rcn-france.org/pour-la-foi-la-patrie-et-la-tradition. https://renaissancecatholique[.]fr. https://archive.ph/vxig7. https://archive.ph/reaqI. https://archive.ph/sxM2F.

<!-- page 19 -->
2.2.3. forsane-alizza.eu, un sixième site lié à l’écosystème lalternative2026.com Le site lalternative2026.com a également été relayé par le groupe Facebook « Forsane Alizza » 58 le 2 mars 2026. Cette page, probablement rachetée par un des opérateurs du MOI Rokh Solis, a été créée le 22 décembre 2022 et a changé de nom le 2 mars 2026. Les investigations de VIGINUM ont permis de détecter deux autres pages également nommées « Forsane Alizza », sur lesquelles figure la même description (« Le jeu n’est pas terminé. Nous sommes là pour changer la Publication de la page Facebook « Forsane Alizza » partageant le site lalternative2026.com France. ») :

> **[Figure F18 - p. 19] Capture de publication Facebook**
>
> Publication de la page Facebook « Forsane Alizza » datée du « 2 mars at 11:46 », avec le texte « Nous sommes fiers de nos militants pour leur engagement positif et leur contribution active à des initiatives constructives au service de la société…. En voir plus » et l'aperçu de lien de lalternative2026.com (en-tête « L'Alternative - Élections Municipales 2026 - Pour une France plus Musulmane », titre « Défendre les intérêts musulmans en France »).
>
> Ce que la figure enseigne : la page « Forsane Alizza » (créée le 22 décembre 2022, renommée le 2 mars 2026, probablement rachetée par un opérateur) relaie le site lalternative2026.com, ce qui relie l'écosystème « islamiste radical » simulé au projet communautariste simulé.

- la communauté « Forsane Alizza » (facebook.com/ForsaneAlizza12), créée le 17 mars 2017 mais dont le nom a changé le 27 octobre 2025 ;

- un compte nommé « Forsane Alizza » 59, au moins actif depuis 2025. Celui-ci était auparavant nommé « Manon Berger » (cf. partie 1.1), et est toujours associé à un compte X @manon_bergers. Ce dernier faisait partie d’un groupe de comptes participant au relais des publications du compte @agirensemblefr sur X (cf. partie 3.1).

En haut à gauche, résultat Google montrant l’ancienne page « Forsane Alizza » ; en bas à gauche, capture d’écran du site forsane-alizza.eu ; à droite, l’une des pages « Forsane Alizza » liée au compte X @manon_bergers.

> **[Figure F19 - p. 19] Captures (résultat Google, site et page Facebook « Forsane Alizza »)**
>
> Trois captures :
> - Résultat Google (haut gauche) : « Facebook - Forsane Alizza (@forsaneAlizzafrance) », « Details ; Page · Community Organization ; forsane-alizza.eu ; Always open ; Not yet rated (0 Reviews) ».
> - Site forsane-alizza.eu (bas gauche) : fond noir, liens « instagram.com/forsanealizza » et « facebook.com/forsanealizzafr », grande calligraphie arabe blanche (de type bannière) et, en jaune, « FORSANE ALIZZA ».
> - Page Facebook « Forsane Alizza » liée au compte X @manon_bergers (droite) : « 655 ami(e)s », silhouette grise par défaut, intro « Le jeu n'est pas terminé. Nous sommes là pour changer la France » suivie de trois drapeaux français ; « Travaille chez Bed Bath & Beyond » ; « A étudié à L'Oréal Paris » ; « Habite à Paris » ; « Célibataire » ; lien X « manon_bergers ».
>
> Ce que la figure enseigne : le faux groupe radical islamiste réactivé repose sur plusieurs pages et comptes Facebook et Instagram portant la même description (« Le jeu n'est pas terminé. Nous sommes là pour changer la France. »), dont l'un est le compte anciennement nommé « Manon Berger » du cluster de relais, et sur le site forsane-alizza.eu (domaine déposé le 14 septembre 2025, IP 80.78.18[.]115 en Suède, une seule image). Les champs d'emploi et d'études incohérents (Bed Bath & Beyond, L'Oréal Paris) trahissent un profil fabriqué.

Les investigations menées sur ce groupe de pages Facebook ont permis à VIGINUM d’identifier des traces d’une ancienne page « Forsane Alizza » dans les résultats de recherche Google. Cette page, aujourd’hui suspendue ou désactivée par les opérateurs du MOI Rokh Solis, participait au relais du nom de domaine forsane-alizza.eu. Ce nom de domaine a été déposé le 14 septembre 2025 sur une adresse IP localisée en Suède (80.78.18[.]115) et ne comporte qu’une seule image, comme le montre déjà une

facebook.com/forsanealizza0 ; ID102718642704535. Cf. archive : https://archive.ph/xK0Yo.

https://web.archive.org/web/20260307132620/https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com

%2Fforsanealizza.

<!-- page 20 -->
première archive datant du 2 octobre 2025. Selon une autre archive du site, prise le 21 octobre 2025, des liens vers des comptes « Forsane Alizza » sur Instagram et Facebook ont été ajoutés : instagram.com/forsanealizza et facebook.com/forsanealizzafr 60. Le 13 octobre 2025, selon un procédé identique à la diffusion coordonnée du nom de domaine piquemalzero.com sur le compte Facebook officiel de François PIQUEMAL, des liens vers le site forsane- alizza.eu ont été déposés dans les commentaires d’une publication sur le compte Facebook officiel de Julien ODOUL, député Rassemblement National pour la 3ème circonscription de l’Yonne et conseiller régional de Bourgogne-Franche-Comté 61.

Au total, entre 13h04 et 13h18, VIGINUM a recensé la mobilisation de 39 utilisateurs inauthentiques ayant chacun diffusé sous cette publication un commentaire. Ces utilisateurs apparaissent tous sous la forme de profils prétendument d’origine vietnamienne, et le comportement de huit d’entre eux tend à confirmer une automatisation de leurs actions sur Facebook. En effet ces huit comptes ont republié le même contenu sur leur propre profil le 13 octobre 2025. Cette publication pourrait correspondre à une phase de test d’automatisation des interactions auprès de contenus postés à partir d’une autre page 62, « AAA Number 2 », qui semble a priori uniquement destinée à cet usage (cf. ci-après).

À gauche, capture d’écran de la page Facebook servant de support aux tests d’automatisation ; à droite, exemples de comptes inauthentiques impliqués par ces tests et diffusant également forsane-alizza.eu en commentaire sur Facebook

> **[Figure F20 - p. 20] Captures de pages Facebook (tests d'automatisation)**
>
> Deux captures.
> - À gauche : page Facebook « AAA Number 2 » (avatar bleu avec un « A » blanc, environ « 24 K followers • 0 suivi(e)s », catégorie « Page · Décoration d'intérieur », « Pas encore évalué (0 avis) »), dont les publications visibles sont des textes sans contenu éditorial : « WW Base 6 » et « WW Base 5 » [dates peu lisibles].
> - À droite : deux profils aux noms en caractères thaïs, chacun avec une republication datée du 13 octobre 2025 d'une publication d'« AAA Number 2 » du 12 octobre 2025 dont le texte est « B2 - 3 ».
>
> Ce que la figure enseigne : la page « AAA Number 2 » semble uniquement destinée à tester l'automatisation d'interactions (le rapport note des publications de type « react 1 30-10 » en 2024 puis une reprise entre le 12 et le 30 octobre 2025) ; huit des 39 comptes de profil prétendument vietnamien qui ont commenté le 13 octobre 2025 (entre 13h04 et 13h18) le lien forsane-alizza.eu sous une publication du député Julien ODOUL ont republié le même contenu, ce qui confirme une action automatisée.

Capture d’écran du message reçu par certains représentants du Rassemblement National sur WhatsApp (source : Atlantico)

> **[Figure F21 - p. 20] Capture de message WhatsApp**
>
> Capture d'un message reçu sur WhatsApp (source citée : Atlantico). Fiche de l'expéditeur : numéro « +44 7546 889549 », nom « ~Forsane Alizza », mention « Origine du numéro de téléphone : Royaume-Uni • N'est pas dans vos contacts • Aucun groupe en commun », boutons « Outils de sécurité » et « Bloquer ». Message en français signé « FORSANE ALIZZA » : le texte affirme que le groupe est « de retour pour combattre et défendre les droits des communautés musulmanes en France et en Europe », qu'il combattra « de toutes nos forces les politiques et les discours qui marginalisent ou criminalisent les musulmans, par tous les moyens », s'adresse à Marine Le Pen, Jean-Philippe Tanguy et Frank Allisio (qualifiés d'« infidèles »), et déclare qu'il n'y aurait plus de place pour les chrétiens en Europe et que le seul moyen d'y parvenir serait le djihad. Le message se termine par « FORSANE ALIZZA est de retour!!! », un verset en arabe [non transcrit], l'adresse « www.forsane-alizza.eu » et l'heure « 17:36 ».
>
> Ce que la figure enseigne : fausse revendication d'un groupe islamiste dissous, envoyée via un numéro britannique à des collaborateurs et élus du Rassemblement National vers le 8 octobre 2025 ; le message renvoie vers forsane-alizza.eu. Il est suivi, le 13 octobre 2025, de l'amplification en commentaires du lien du site sous une publication de Julien ODOUL, ce qui constitue une séquence d'intimidation puis de relais destinée à provoquer une réaction politique et médiatique.

https://web.archive.org/web/20251021082418/https://forsane-alizza.eu/. https://archive.ph/aQxey. https://www.facebook[.]com/profile.php?id=61558999180069 - https://archive.ph/4oDUg, créée le 3 mai 2024. Du 24 septembre au 30 octobre 2024, les publications de cette page se présentent sous la forme de tests, comprenant le nom d’une action et une date (par exemple, « react 1 30-10 »). Après un an sans activité, cette page reprend le même type de comportement entre le 12 et le 30 octobre 2025.

<!-- page 21 -->
Cette manœuvre d’amplification semble intervenir quelques jours après une première action auprès de plusieurs « collaborateurs et élus du groupe Rassemblement National à l’Assemblée [qui] ont eu la mauvaise surprise de recevoir sur leurs numéros de téléphone personnels, via la messagerie WhatsApp, des messages inquiétants signés « Forsane Alizza » », comme le révèle un article publié par le média Atlantico le 8 octobre 2025 63.

Le texte qui constitue le message, diffusé à l’aide d’un numéro britannique sur WhatsApp, présente un narratif similaire à celui visible sur le site forsane-alizza.eu. Celui-ci annonce le prétendu retour de ce groupe radical islamiste « pour combattre et défendre les droits des communautés Musulmanes », tout en accusant les représentants du Rassemblement National de « faire taire les voix Musulmanes en France et en Europe ».

https://atlantico.fr/article/decryptage/menaces-islamistes-sur-les-deputes-une-operation-russe-rassemblement-national- forsane-alizza-gabriel-robin.

<!-- page 22 -->
3. IMPLICATION D’UN ACTEUR ÉTRANGER

Les investigations menées par VIGINUM autour des actifs numériques constituant le MOI Rokh Solis ont conduit à l’identification de plusieurs éléments techniques permettant d’émettre l’hypothèse de l’implication d’un acteur étranger. Les différents marqueurs d’extranéité identifiés se situent aussi bien au niveau de l’infrastructure numérique du dispositif qu’au niveau des plateformes de réseaux sociaux (schéma de diffusion et tentative d’amplification des contenus mis en ligne).

3.1. Des relais inauthentiques communs avec une organisation pro- israélienne Le compte X @LeBlogdeSophie0 présente plusieurs caractéristiques inauthentiques : 403 de ses 416 abonnés ont été créés le 4 mars 2026, et parmi eux, 401 comptes n’ont aucun abonné. Lors d’une première analyse le 5 mars 2026, le compte X @LeBlogdeSophie0 affichait 416 abonnés. Or, probablement après une vague de suspension de comptes, le 8 mars 2026, ce même compte n’affichait plus que 13 abonnés. Neuf des abonnés restants 64 appartiennent à un réseau de comptes inauthentiques sur X, dont l’activité est exclusivement orientée vers le relais de publications en langue française de l’organisation pro- israélienne ELNET et du compte X @agirensemblefr 65. Ces comptes aux caractéristiques inauthentiques (images générées par IA, incohérence de langage entre les premières et dernières publications) ont, pour la plupart, changé leur username en août 2025 (à l’instar du compte TikTok @renaissancecatholiquefr) et présentent des liens avec l’un des groupes de comptes inauthentiques identifiés sur Facebook (cf. partie 2.1.6). L’analyse de cette activité inauthentique a permis à VIGINUM d‘identifier l’existence de 17 comptes appartenant à ce même cluster (cf. annexe 5.4). Ces derniers ont également publié des réponses sous une publication X du média Sud Radio traitant du sujet de lalternative2026.com 66 : deux d’entre eux ont repartagé une URL de lalternative2026.com en commentaireet le dernier a diffusé une capture d’écran d’une publication Facebook de « Forsane Alizza » 67. Cette capture d’écran montre par ailleurs que le compte @its_anasantos utilise une version anglophone de Facebook. Par ailleurs, VIGINUM a observé que le compte X de cette liste @LeaaaLeclerc, indique un profil similaire à celui identifié sur Facebook @lea.leclerc51 68, notamment avec le recours aux mêmes photos de profil et de couverture. Tandis que le compte X participait au relais des contenus des actifs numériques liés au « Blog de Sophie », le compte Facebook s’est mobilisé pour la diffusion d’une publication de la page Facebook « Je suis François Piquemal et je ne suis rien. » le 19 février 2026, attestant d’un lien technique supplémentaire entre les deux opérations. En outre, les quelques commentaires présents sur des publications des comptes @drole_230 et @renaissancecatholiquefr ont été publiés par une dizaine de comptes ayant aimé les mêmes publications des comptes d’@agir_ensemble_, @justefrançais @drole_230 et @renaissancecatholiquefr. Ainsi, sur X, TikTok et Facebook, les publications provenant de comptes du MOI Rokh Solis semblent soutenues par des réseaux de comptes inauthentiques liés entre eux et déjà impliqués dans le relais de publications en lien avec ELNET.

@ErnestEnchelom, @Adrienubois, @AlexandrMoreau, @AmeliCharpente, @LeaaaLeclerc, @manon_bergers, @Theo0Blanc, @Celinejtaime, @EscofilPericles. Agir Ensemble a été lancée par l’organisation pro-israélienne ELNET après les attaques terroristes du 7 octobre 2023 en Israël. https://archive.ph/3SRX0. https://archive.ph/QbM1m ; https://archive.ph/UNDV7 ; https://archive.ph/6H7VJ. https://archive.ph/bSfAR. Sur cette page Facebook, se trouve encore la trace de son compte Instagram, aujourd’hui suspendu : @lea_6lecrec.

<!-- page 23 -->
3.2. Des éléments techniques comportant certains marqueurs d’extranéité L’analyse des données en sources ouvertes présentes sur les différents sites du MOI Rokh Solis a permis de relever plusieurs marqueurs d’extranéité, confirmant a minima l’implication indirecte d’un acteur étranger. En effet, l’analyse technique des métadonnées du site lalternative2026.com a mis en lumière la présence de caractères hébreux dans certains titres de fichiers .png.

Présence de caractères hébreux dans les métadonnées du site lalternative2026.com.

> **[Figure F22 - p. 23] Tableau (métadonnées de fichiers)**
>
> Tableau à deux colonnes : à gauche le nom de fichier .png téléversé sur lalternative2026.com, à droite (surligné en jaune) le titre contenu dans les métadonnées, qui se termine par un suffixe en hébreu « גרפיקות לעמוד » (traduction indicative de l'annotateur : « graphismes de la page »).
>
> | Fichier | Titre dans les métadonnées |
> |---|---|
> | Autonomie-communautaire.png | Autonomie communautaire - גרפיקות לעמוד |
> | Autorisation-du-port-de-signes-religieux.png | Autorisation du port de signes religieux - גרפיקות לעמוד |
> | Autorisation-et-extension-de-lappel-a-la-priere.png | Autorisation et extension de l'appel à la prière - גרפיקות לעמוד |
> | Construction-de-mosquees-et-de-minarets.png | Construction de mosquées et de minarets - גרפיקות לעמוד |
> | Instauration-de-systemes-juridiques-religieux-distincts-notamment-lapplication-de-la-charia.png | [début tronqué] de systèmes juridiques religieux distincts (notamment l'application de la charia) - גרפיקות לעמוד |
> | Legalisation-de-structures-separees-par-genre-piscines-centres-sportifs-hopitaux.png | Légalisation de structures séparées par genre (piscines, centres sportifs, hôpitaux) - גרפיקות לעמוד |
> | Reduction-du-controle-de-lEtat-dans-les-structures-educatives-distinctes.png | Réduction du contrôle de l'État dans les structures éducatives distinctes - גרפיקות לעמוד |
>
> Ce que la figure enseigne : marqueur d'extranéité technique. Les pictogrammes du site lalternative2026.com ont été produits dans un outil de conception dont l'interface ou l'utilisateur est en hébreu, et le titre de chaque image porte la même mention hébraïque ; le rapport en déduit au minimum l'implication indirecte d'un acteur étranger. Le schéma 5.2 relie ces fichiers à des comptes Canva (voir figure de l'annexe 5.2).

De plus, VIGINUM a identifié un utilisateur Canva dans les métadonnées du site forsane-alizza.eu qui serait localisé en Israël, et qui présente dans son nom des caractères en hébreu 69. Par ailleurs, les investigations de VIGINUM démontrent que le compte TikTok @renaissancecatholiquefr 70 comporte des éléments de localisation en Israël, et que son nom a été modifié le 3 août 2025 71. Or, entre le 29 novembre 2024 et le 18 juin 2025, ce compte a diffusé plusieurs dizaines de vidéos anglophones aux narratifs anti-musulmans et anxiogènes, mettant en scène des attaques terroristes sur le sol européen. Ces vidéos diffusaient toutes des hashtags #TheWestIsNext ou #TheWestIsNow 72 et des appels à la mobilisation promus par les sphères pro-israéliennes depuis les attaques terroristes du 7 octobre

2023. Si le compte @renaissancecatholiquefr semble avoir changé de nom entre sa création et le déploiement de cette campagne, le fichier Localisation du compte TikTok audio lié à ce compte semble toujours porter la marque de son ancien @renaissance.catholique en Israël nom (@IrelandRealNews 73). selon l’outil omar-thing.site

> **[Figure F23 - p. 23] Capture d'outil d'analyse TikTok (omar-thing.site)**
>
> Fiche du compte TikTok « renaissance.catholique » (@renaissancecatholiquefr) telle qu'affichée par l'outil omar-thing.site : localisation « Israel (IL) », langue « English », bio « Foi, héritage et fierté nationale - Défendre nos racines catholiques » avec un drapeau français. Statistiques : 112,449 followers ; 444 following ; 431,259 hearts ; 82 videos ; 82 friends. Détails : User ID 7413670193391764522 ; Private : No ; Created : 2024-09-12 08:28:37 ; Nickname Edited At : 2025-08-03 20:40:52 ; Username Changed At : N/A.
>
> Ce que la figure enseigne : le compte qui incarne le pôle « souverainiste catholique » francophone a été créé le 12 septembre 2024 et a changé de nom le 3 août 2025 ; l'outil le localise en Israël, ce qui est l'un des marqueurs d'extranéité du rapport (le compte comptabilise 112 500 abonnés).

Description d’une vidéo TikTok de @renaissancecatholiquefr laissant apparaitre la mention « Ireland Real News ».

> **[Figure F24 - p. 23] Capture de publication TikTok annotée**
>
> Description d'une vidéo TikTok du compte renaissancecatholiquefr (nom affiché « renaissance.catholique », date 2024-11-30) : hashtags « # TheWestIsNext # london2030 », lien Facebook (« https://www.facebook.com/share/v/1BWvQcU65p/ », « Voir l'original »), étiquette de lieu « Cité de Londres ». Un cadre rouge ajouté par VIGINUM entoure la ligne du son : « original sound - Ireland Real News - renaissance.catholique ».
>
> Ce que la figure enseigne : trace de l'ancien nom du compte (@IrelandRealNews) conservée dans le fichier audio ; le compte, créé en septembre 2024, diffusait entre le 29 novembre 2024 et le 18 juin 2025 des vidéos anglophones anxiogènes et anti-musulmanes sous les hashtags #TheWestIsNext / #TheWestIsNow, avant d'être renommé et réorienté vers l'audience francophone, ce qui montre le recyclage d'un actif d'influence d'une campagne anglophone vers une campagne en français.

Le compte Canva aurait été créé le 14 septembre 2025 : « username : dolipart ; brand: BAGy77qLSTo ; branddisplayName ‫הצוות‬ ‫ של‬Dolipart ». Le compte comptabilise 112 500 abonnés et 430 000 mentions « j’aime ». Utilisation croisée des outils omar-thing.site et Exolyt. Le hashtag #TheWestIsNext fait partie des récits diffusés et promus par des sphères pro-israéliennes à partir d'octobre 2023, à destination d'audiences occidentales, présentant Israël comme le seul rempart contre le Hamas, dont l'Europe et l'Occident seraient les prochaines cibles. https://www.tiktok[.]com@renaissancecatholiquefr/video/7442494720040815915.

<!-- page 24 -->
De plus, les requêtes effectuées sur Google à partir des anciennes publications TikTok de @renaissancecatholiquefr ont permis de retrouver la trace de comptes aujourd’hui supprimés ou suspendus sur Facebook et Instagram. Ces derniers semblent également avoir participé à une campagne de diffusion des hashtags #TheWestIsNext ou #TheWestIsNow, entre la fin de l’année 2024 et juin 2025, auprès d’une audience anglophone.

Par exemple, parmi les résultats obtenus à partir de la requête « Is London going down ? Is this our future ? What do you guys think ? #uklondon2030 #thewestisNOW », apparaît un compte Instagram, @trueirishvoice 74. Aujourd’hui inaccessible, ce compte semble toujours posséder un équivalent du même nom sur TikTok 75, également localisé en Israël. Ces éléments supposent que les opérateurs du MOI Rokh Solis auraient visé d’autres audiences par le passé, notamment en Irlande et en Angleterre. De surcroît, les autres comptes TikTok liés au MOI Rokh Solis, @elections_municipales 76 et @justefrancais sont quant à eux localisés au Royaume-Uni. Cette localisation renforce l’hypothèse de l’implication d’un acteur étranger remobilisant des comptes impliqués dans des campagnes informationnelles pro-israéliennes visant plusieurs pays, dont le Royaume-Uni. Localisation du compte @trueirishvoice en Israël selon l’outil omar-thing.site

> **[Figure F25 - p. 24] Capture d'outil d'analyse TikTok (omar-thing.site)**
>
> Fiche du compte TikTok « True Irish Voice » (@trueirishvoice), avec drapeau irlandais dans le nom et avatar d'un homme barbu en veste verte sur fond de drapeau irlandais (aspect de rendu généré) : localisation « Israel (IL) », langue « English », bio « Real voices. Real stories. » avec les drapeaux britannique et irlandais. Statistiques : 30,102 followers ; 0 following ; 6,455 hearts ; 3 videos. Détails : User ID 7513684512015991815 ; Private : No ; Created : 2025-06-08 21:05:44 ; Nickname Edited At : 2025-08-22 14:23:22 ; Username Changed At : N/A.
>
> Ce que la figure enseigne : ce compte, équivalent TikTok du compte Instagram @trueirishvoice retrouvé via une requête Google sur d'anciennes publications (hashtags #thewestisNOW, #uklondon2030), est lui aussi localisé en Israël ; le rapport en déduit que les opérateurs ont visé par le passé des audiences irlandaise et britannique. Il note que les comptes TikTok @elections_municipales (ex-@drole_230) et @justefrancais sont localisés au Royaume-Uni.

3.3. Une entreprise israélienne probablement à l’origine des campagnes VIGINUM a identifié l’existence d’une entreprise israélienne œuvrant dans le domaine de l’influence nommée Blackcore. Cette conclusion a été confirmée par les publications des médias Reuters 77, Libération 78 et Haaretz 79. Les investigations conduites au sujet de cette dernière ont permis de caractériser une infrastructure numérique liée à cette pseudo entreprise, proposant des services orientés vers l’amplification artificielle de contenus en ligne et la conduite d’opérations informationnelles inauthentiques. Cependant, cette entité semble s’insérer dans un écosystème composé de nombreuses entreprises israéliennes du secteur cyber, ce qui empêche à ce stade de déterminer avec un niveau de confiance suffisant l’identité et le périmètre précis de l’action de l’entreprise privée à l’origine du MOI Rokh Solis. Aucun élément ne permet par ailleurs à VIGINUM d’établir l’identité et l’origine des commanditaires ayant pu recourir aux services de cette société.

3.3.1. Blackcore, une entité israélienne sans statut légal dont les stratégies se rapprochent des campagnes observées La pseudo-société Blackcore ne produit que très peu de données en source ouverte, ce qui ne permet pas à VIGINUM de vérifier les statuts légaux de cette entité, a priori, non inscrite au registre des entreprises israéliennes. Pour autant, un nom de domaine, blackcore.online, a été déposé le 22 août

https://archive.ph/A6Dun. https://archive.ph/HTHLQ. Le compte TikTok @drole_230 a changé son nom d’utilisateur le 9 mars 2026 pour devenir @elections_municipales. https://www.reuters.com/business/media-telecom/france-probes-whether-israeli-firm-blackcore-interfered-local-elections- sources-2026-05-13/. https://archive.ph/vZ3wc. https://archive.ph/fK5Cr.

<!-- page 25 -->
2025 80, date correspondant à l’activation de certains actifs numériques du MOI Rokh Solis, notamment les changements d’usernames des comptes inauthentiques amplificateurs sur X (cf. partie 3.1.). Sur son site, Blackcore se présente comme une entreprise délivrant des solutions d’influence, de sécurité et de technologies au service de gouvernements. L’entreprise possède également un compte LinkedIn (@officialblackcore) 81, disposant d’une seule publication datant d’octobre 2025 82 et comptabilisant 32 réactions issues d’un réseau de comptes inauthentiques (cf. partie 3.3.3).

En outre, plusieurs sous-domaines sont associés au nom de domaine blackcore.online :

- angola-plan.blackcore.online 83 fait notamment référence à une formation donnée au gouvernement d’Angola en février 2026, laissant supposer que l’entreprise israélienne a directement traité avec ce gouvernement 84. VIGINUM émet l’hypothèse d’un lien existant entre le réseau de comptes inauthentiques identifié en partie 2.1.6 amplifiant des contenus du gouvernement angolais et l’activité de Blackcore avec ce même gouvernement. Cette hypothèse est notamment appuyée par des liens techniques d’infrastructure web analysés en partie 3.3.2.

- demo.blackcore.online 85 détaille la stratégie employée par l’entreprise pour mener ses opérations d’influence. Blackcore revendique notamment détenir un large réseau de comptes inauthentiques sur Facebook, Instagram et TikTok (1 600 avatars cumulés). Blackcore prétend également être en mesure de faire reposer sa stratégie d’influence sur l’envoi massif de messages personnels et l’intégration de communautés (notamment des groupes Facebook - une stratégie déjà identifiée par VIGINUM dans le cadre des manœuvres liées au MOI Rokh Solis, cf. partie 2.1.6).

- full-report.blackcore.online 86 affiche le logo de Blackcore contenant des métadonnées en hébreu (‫ )נתי מסעוד‬correspondant au nom Nati MASOOD. Un profil Facebook (@nati.masood.5) appartenant à Nati Eliyahu MASOOD 87, qui se présente comme citoyen israélien, pourrait correspondre à ces métadonnées. Ce dernier semble être un développeur créant des sites 88 et des outils d’automatisation 89.

3.3.2. Une infrastructure révélant un large éventail d’outils d’amplification artificielle liés aux opérations ciblant les élections municipales Parmi les autres sous-domaines de blackcore.online, proposals.blackcore.online est hébergé sur l’adresse IP 94.237.57[.]56, sur laquelle ont été déposés trois noms de domaines : balckcore.com, electric- marinade.com et omrisystems.com 90. Ces deux derniers permettent de remonter à une liste importante de sous-domaines (cf. annexe 5.5.), qui semblent exclusivement servir d’interfaces à des outils d’amplification artificielle, par exemple :

Le site web a été supprimé suite à la publication des articles de mai 2026 mentionnés précédemment. Le compte LinkedIn a également été supprimé suite à la parution des articles. https://archive.ph/0kOnB. https://archive.ph/WQA4N. Les autorités angolaises ont, par le passé, déjà traité avec des entreprises israéliennes pour mener des campagnes informationnelles. Cf. le rapport du Stanford Internet Observatory de 2022 sur les opérations de l’entreprise israélienne Mind Force en Angola https://archive.ph/aHvYx). https://archive.ph/vRSAo. http://web.archive.org/web/20260513153840/https://full-report.blackcore.online/. https://archive.ph/tBP5f. https://archive.ph/255VV. https://archive.ph/FfmtB. Ces sites web ont également été supprimés suite à la parution des articles de presse.

<!-- page 26 -->
- fb-search.omrisystems.com semble donner accès à une interface permettant de trouver via un LLM et à partir d’une requête, les mots clés associés les plus pertinents. Ces derniers sont ensuite utilisés automatiquement pour trouver tous les principaux groupes Facebook liés à cette requête ;

- avatar-data-generator.electric-marinade.com et avatar-data-generator.omrisystems.com sont probablement des applicatifs de gestion d’avatars. Ces derniers pourraient avoir été développés par l’entreprise israélienne Galacticos AI, comme le Aperçu de l’interface proposée à partir de fb- souligne le titre associé à la page avatar-data- search.omrisystems.com generator.omrisystems.com : « Login – Avatar Data Generator by Galacticos AI ». Par ailleurs, une autre entité cyber a été identifiée grâce à cet élément technique, du nom d’Iron Mind (avatar-data-generator.dev.iron-mind.ai). Les éléments techniques identifiés autour de cette entité ont permis de remonter au nom de domaine iron-mind.ai 91. Vraisemblablement spécialisée dans le développement de logiciels, la société sur laquelle repose les services numériques présentés sur iron-mind.ai, nommée Iron Mind AB, a été enregistrée en Suède par Nir DOBICKY le 30 octobre 2025 92. Nir DOBICKY 93, individu se présentant comme d’origine israélienne né en avril 1980, est également à la tête de plusieurs sociétés enregistrées au Royaume-Uni et en Suède (notamment Rocketpod Technologies LTD 94 et Blade Marketing, également nommée Social Worx LTD 95). Ces entreprises sont spécialisées dans les services d’influence en ligne principalement via des outils d’automatisation, notamment sur LinkedIn, plateforme également concernée par des activités d’amplification artificielle rattachables à Blackcore (cf. partie 3.3.3).

> **[Figure F26 - p. 26] Capture d'interface d'outil (fb-search.omrisystems.com)**
>
> Interface intitulée « AI Facebook Group Finder » : sous-titre « Describe what you're looking for - let AI do the searching. », champ « What's your goal or interest? » avec le texte d'exemple « e.g., find active groups about remote freelance web development » et bouton « Start Search ».
>
> Ce que la figure enseigne : outil du sous-domaine fb-search.omrisystems.com qui, à partir d'une requête, utilise un LLM pour trouver les mots-clés pertinents puis retrouver automatiquement les principaux groupes Facebook correspondants. C'est la brique de ciblage des communautés à infiltrer, cohérente avec la tactique observée (comptes inauthentiques intégrant des dizaines de groupes Facebook avant de relayer les contenus du MOI).

Capture d’écran des pages d’accueil de social-worx.com (à gauche) et rocket-pod.ai (à droite)

> **[Figure F27 - p. 26] Captures de sites commerciaux**
>
> Deux pages d'accueil.
> - À gauche, social-worx.com (logo « SOCIALWORX ») : titre « GROW YOUR INSTAGRAM & TIKTOK », sous-titre « Grow your Instagram with real device-based engagement », bouton « SEE PLANS », mention « SECURE LOGIN • REAL DEVICES • CANCEL ANYTIME » et trois chiffres de réussite (environ « 12M+ Actions performed », « 450K+ Followers generated », « 300+ Active clients » [chiffres peu lisibles]), avec une maquette de téléphone.
> - À droite, rocket-pod.ai (logo « RocketPod ») : titre « GROW YOUR LinkedIn », « Trusted by 7500+ users », bouton « GROW YOUR LINKEDIN NOW », « Welcome to RocketPod », illustration de fusée.
>
> Ce que la figure enseigne : deux sociétés de Nir DOBICKY (Blade Marketing/Social Worx LTD et Rocketpod Technologies LTD) vendent ouvertement de l'engagement artificiel automatisé sur Instagram, TikTok et LinkedIn ; ce sont des fournisseurs d'outils d'amplification liés à l'écosystème Blackcore (voir schéma 5.5).

En outre, grâce aux investigations conduites sur l’infrastructure web autour d’electric-marinade.com, VIGINUM a détecté des traces d’une autre campagne ciblant une audience pro-palestinienne. En effet, le serveur de messagerie (mail server) associé à electric-marinade.com, smtp.electric-marinade.com,

Ce nom de domaine n’a pas été évoqué dans la presse et les sites web qui lui sont rattachés sont, quant à eux, à date, toujours actifs. https://archive.ph/37avF ; société n°559551-9181 ; adresse : Kopparvägen 44, 187 45 Täby). Nir DOBICKY se fait également appeler Niro Knox sur ses réseaux sociaux. https://archive.ph/mGtUG. RocketPod est notamment l’entreprise ayant développé CommenTron, un outil d’automatisation de commentaires LinkedIn (https://archive.ph/xGq9n.) https://archive.ph/8dHyB.

<!-- page 27 -->
hébergé sur l’adresse IP 5.22.213[.]198 est, également lié au nom de domaine sadaqahpalestine.com 96. Aujourd’hui inactif, ce site faisait la promotion d’une pseudo-organisation caritative en faveur de la population palestinienne nommée « Sadaqah Palestine » 97. Plusieurs comptes sont associés à ce nom de domaine sur X, Facebook et Instagram 98 (seul le compte X est encore actif à ce jour). L’analyse de l’activité du compte X @sadaqahpalestin, a révélé l’existence d’un groupe de comptes participant à l’amplification de ses publications. Celles-ci ont toutes bénéficié de la publication de commentaires par des comptes dont les caractéristiques présentent de nombreux marqueurs d’inauthenticité et un comportement identique aux comptes identifiés dans la publication de commentaires sur les contenus présents au sein des pages Facebook ciblant Sébastien DELOGU, François PIQUEMAL et David GUIRAUD :

- production artificielle de commentaires générés par l’intermédiaire d’un LLM ;

- utilisation de plusieurs langues dans le cadre de leur activité globale (anglais, français, espagnol, portugais et arabe) ;

- dates de création des comptes et rythme de publication indiquant une automatisation de ces actions par l’opérateur de ce groupe ;

- profils féminins ou masculins, jeunes, reprenant des noms à consonnance anglophone et présentant pour certains des photos de profils générées par intelligence artificielle.

Exemple de publication de @sadaqahpalestin sur X ayant bénéficié d’une activité inauthentique.

> **[Figure F28 - p. 27] Capture de profil et de publication X (activité inauthentique)**
>
> Deux captures du compte X « Sadaqah Palestine » (@SadaqahPalestin).
> - Profil : logo vert « SADAQAH PALESTINE » avec calligraphie arabe ; bannière en niveaux de gris montrant une femme portant un enfant ; bio « Humanitarian aid for Palestine | Supporting families, refugees & children | Non-profit | Providing aid, medical care & education with compassion. » ; catégorie « Non-Governmental & Nonprofit Organization », lieu « Palestine », lien sadaqahpalestine.com, « Joined December 2012 », 1,539 Following, 842 Followers.
> - Publication du 16 février 2025 à 16h15 (« 212 Views », 17 réponses, 3 republications, 82 mentions j'aime) : « Gaza is running out of time. No food. No water. No medical supplies. No safe zones. Hospitals are collapsing, operating without anesthesia. Families dig through rubble searching for their loved ones. Starvation is setting in. The world must not stay silent. » Réponses du 20 février 2025 : « Jada Weber » (@ada_weberaj) « how do i donate directly? », « Agnes Hansen » (@AHansengens) « this broke my heart helping now », « Amy Morrison » (@PolevamaTa) « just donated, hope it helps », « Joan Vinson » (@joan_vinsona) « no food, no water... this is insane », chacune avec 1 à 3 vues.
>
> Ce que la figure enseigne : une fausse organisation caritative pro-palestinienne (domaine sadaqahpalestine.com, désormais inactif) dont les publications reçoivent des commentaires courts, génériques et émotionnels de comptes à noms anglophones et photos de profil attrayantes, avec des marqueurs d'inauthenticité identiques à ceux des relais des pages anti-LFI (commentaires générés par LLM, multilinguisme, création et rythme automatisés). Ces mêmes comptes commentent le compte X de Nir DOBICKY (@niroknox) et ont aussi visé le Premier ministre écossais John SWINNEY et le SNP (au moins 256 comptes, 1 400 commentaires entre le 6 janvier et le 8 mai 2026).

Or, ces comptes sont également présents dans les commentaires de certaines publications du compte X de Nir DOBICKY, @niroknox 99. Par ailleurs, si l’activité principale de ces comptes concerne l’amplification de publications traitant de thématiques lifestyle ou d’actifs chiffrés, ils semblent cependant être mobilisés ponctuellement sur des opérations plus spécifiques comme « Sadaqah

Seuls trois noms de domaines sont associés à ce serveur mail, dont electric-marinade.com et sadaqahpalestine.com. Cette expression pourrait faire référence aux projets conduits par des instances comme Muslims Around The World (MATW) ou l’Office de secours et de travaux des Nations unies pour les réfugiés de Palestine dans le Proche-Orient (UNRWA), faisant référence à la notion de charité, sadaqah en arabe (‫)ﺻﺪﻗﺔ‬. @SadaqahPalestin sur X (https://archive.ph/WClbg) ; @sadaqahpalestine sur Instagram ; « Sadaqah Palestine » sur Facebook (facebook.com/sadaqahpalestine) ; https://web.archive.org/web/20260521132548/https://www.google.com/web/2026052113254 8/https://www.google.com/search?q=%22sadaqah+palestine%22+facebook&sxsrf=ANbL- n7raeB1PiAzKLG5URstT7H2PAdUEg%3A1779369192632&sei=3AcPaoXLEJ3CkPIPzI-fqQU. https://ghostarchive.org/archive/rgvtX ; https://ghostarchive.org/archive/RRRol ; https://ghostarchive.org/archive/3ttx7.

<!-- page 28 -->
Palestine » ou plus récemment au sujet des dernières élections en Écosse. En effet, le compte du Premier ministre écossais, John SWINNEY, ainsi que celui de son parti, le Scottish National Party (SNP), semblent avoir été ciblés à quatre reprises entre le 6 janvier et le 8 mai 2026, par la publication coordonnée de commentaires en anglais issus de ces comptes inauthentiques. Dans le cadre de cette opération, VIGINUM a identifié la mobilisation d’au moins 256 comptes ayant permis la diffusion de 1 400 commentaires, principalement sur les publications de @JohnSwinney, @theSNP et @ScotGovFM (respectivement 652, 338 et 112 commentaires). De surcroît, l’exploitation du logo présent sur le compte X @sadaqahpalestin a permis d’identifier, via un avis sur la plateforme Fiverr, le profil « paullussac » qui serait à l’origine d’une demande de prestation pour la création de ce logo 100.

Avis laissé par « paullussac » sur Fiverr

> **[Figure F29 - p. 28] Capture d'avis sur plateforme de freelances (Fiverr)**
>
> Avis laissé sur Fiverr par le profil « paullussac » (pays affiché : Pays-Bas) : note de 2 étoiles sur 5, « 1 year ago », texte « The response time was too long and communication was not optimal. », prix « Up to US$50 », durée « 5 days ». L'image jointe à l'avis est le logo vert « SADAQAH PALESTINE » avec calligraphie arabe, sur fond filigrané.
>
> Ce que la figure enseigne : l'avis public a permis de relier le logo du compte X @sadaqahpalestin à une commande de création de logo passée par « paullussac » ; ce profil a aussi laissé des avis pour des vidéos IA destinées à des audiences togolaise et gabonaise et pour un site de la société israélienne twostepinnovations.com, hébergée sur les mêmes adresses IP que blackcore.online.

Les investigations sur ce profil ont révélé la présence d’autres avis laissés par ce dernier, notamment pour la génération de vidéos par intelligence artificielle qui seraient destinées à des audiences togolaise ou gabonaise 101. L’un des avis laissés par « paullussac » renvoie également vers la création d’un site internet pour une société localisée en Israël : twostepinnovations.com 102. Or, ce nom de domaine est hébergé sur les mêmes adresses IP sur lesquelles se trouvait blackcore.online 103. Enfin, VIGINUM a relevé la présence d’un numéro de téléphone sur le site sadaqahpalestine.com. Les investigations menées autour de ce numéro ont permis l’identification d’un compte TikTok, @deepdiveafrica, créé le 10 avril 2025 et indiquant une localisation en Israël 104. Tandis qu’une publication de @deepdiveafrica fait la promotion de l’ouverture d’un hôpital israélien au Togo à des fins humanitaires 105, une autre de ses publications affiche un logo sur lequel est inscrit « Politizando Angola » 106.

Par ce biais, VIGINUM a identifié un compte Instagram toujours actif, @politilizando_angola, créé en janvier À gauche, utilisation de l’outil omarthing.site ; à 2026 107, ainsi qu’une page Facebook inactive « Politizando droite, logo « Politizando Angola » dans une vidéo du compte TikTok @deepdiveafrica

> **[Figure F30 - p. 28] Captures de compte et de vidéo TikTok (@deepdiveafrica)**
>
> Deux éléments.
> - À gauche, fiche omar-thing.site du compte « Deep Dive Africa » (@deepdiveafrica) : région « Philippines (PH) », « Locked Region : Israel (IL) », langue « English », bio « Stories you won't see on the front page. Culture • News • Underground Africa. See more than headlines. » ; 73,482 followers ; 55 following ; 497,335 hearts ; 84 videos ; 3 friends ; User ID 7434173136931849248 ; Created 2025-04-10 21:26:19 ; Nickname Edited At 2025-08-09 12:43:02 ; Username Changed At N/A ; Support/Gift Level Lv.0.
> - À droite, deux images extraites d'une vidéo du compte : en haut, un badge rond « POLITIZANDO ANGOLA » avec la carte de l'Angola, un emblème et des micros ; en dessous, une scène d'aspect généré par IA intitulée « WORLD GOVERNMENTS SUMMIT » avec un homme en costume sur une scène et une bannière au texte déformé [illisible].
>
> Ce que la figure enseigne : le compte TikTok, trouvé grâce au numéro de téléphone affiché sur sadaqahpalestine.com, est localisé en Israël (région verrouillée Israël), a été créé le 10 avril 2025 et diffuse du contenu pro-gouvernemental sur l'Afrique (un hôpital israélien au Togo) ainsi que le logo « Politizando Angola », qui mène à un compte Instagram actif (@politilizando_angola, créé en janvier 2026) et à une page Facebook inactive du même nom. Le compte TikTok @africa_real_news présente un fil quasi identique et une localisation israélienne. Ce chaînage est l'un des liens qui rattachent l'opération angolaise et les actifs Sadaqah Palestine à l'infrastructure de Blackcore et au MOI Rokh Solis.

https://www.fiverr.com/ayeshamalik100/design-modern-and-professional-business-logo-and-branding. https://www.fiverr.com/firna_moh/create-ai-story-animations-and-lip-sync-video-using-ai ; https://br.fiverr.com/zinzir/spray- paint-your-logo-or-image-or-nft-on-a-graffiti-wall-4k-animated-video. https://www.fiverr.com/eyaakubhossain. 185.230.63[.]107 ; 185.230.63[.]171 ; 185.230.63[.]186 et 34.149.87[.]45. https://archive.ph/ON9zf. https://archive.ph/BVlx8. https://archive.ph/sYBjS. https://archive.ph/Es26c.

<!-- page 29 -->
Angola » 108. Les recherches conduites sur cette page Facebook ont montré que celle-ci s’était intégrée dans les mêmes groupes que ceux identifiés pour les comptes Facebook inauthentiques ciblant l’Angola (cf. partie 2.1.6). Enfin, un autre compte TikTok, @africa_real_news 109 présente un fil de publication quasiment identique à celui de @deepdiveafrica et indique également une localisation en Israël. L’identification de liens techniques entre ces opérations et l’infrastructure web de Blackcore, permet, notamment par les procédés d’amplification détectés, de rattacher ces éléments au MOI Rokh Solis et aux opérations d’ingérence numérique étrangère analysées dans le cadre des élections municipales en France.

De plus, les investigations conduites sur l’adresse IP 94.237.57[.]56 mettent en lumière plusieurs ports ouverts sur le serveur, dont le port 3002 auquel une API est associée. La configuration de ce serveur permet d’établir des liens entre les noms de domaine electric-marinade.com, omrisystems.com et éventuellement sni.ai, comme le suggère le sous domaine sni-staging 110. À ce stade, les entités du nom de Blackcore, Electric Marinade et Omri systems ne semblent pas avoir d’existence légale, à la différence de Galacticos AI, SNI et Iron-Mind.

Parmi les rares mentions qui sont faites en ligne de Galacticos AI, VIGINUM a identifié une publication issue du site Internet d’un cabinet d’avocats spécialisé notamment en affaires internationales : Afik & Co 111. Ce dernier est localisé au 103 Hachashmonaim à Tel Aviv, et a été fondé par Doron AFIK 112. L’article en question, publié le 21 septembre 2025 113 sur afiklaw.com, revient sur l’organisation d’un « cocktail diplomatique » dans leurs locaux. Les photos de cet évènement, publiées dans l’article et sur Facebook 114, témoignent de la présence d’un individu du nom de Yigal UNNA 115, a priori invité en tant que représentant de l’entreprise Galacticos AI (cf. partie 3.3.3). Or, une société du nom de Galacticos LTD 116 est domiciliée à la même adresse que le cabinet Afik & Co. Selon le site checkid.co.il, Doron AFIK ferait partie de la direction de Galacticos LTD 117. L’entreprise dispose également d’un nom de domaine galacticos.ai, inaccessible à ce jour, et enregistré le 17 avril 2024 par Guy GEYOR via la société SNI, également domiciliée au 103 Hachashmonaim à Tel Aviv. Guy GEYOR semble connaître Doron AFIK, puisque les investigations conduites sur son compte LinkedIn 118 le montrent au moins à deux reprises en photo avec ce dernier au cours de l’année 2025. Nir DOBICKY parait également lié à la société Afik & Co., puisqu’il a été identifié à plusieurs reprises sur des publications du compte Instagram de la société 119. De plus, Doron AFIK, Guy GEYOR et Nir DOBICKY se suivent mutuellement sur Instagram. Enfin, au moins deux publications 120 sur LinkedIn, liées respectivement au cabinet d’avocats Afik & Co. et à Blackcore, semblent avoir bénéficié d’une amplification artificielle par un même groupe d’une trentaine de comptes a priori inauthentiques (cf. annexe 5.6.).

https://facebook.com/61586801304537. https://archive.ph/41Lpg. Configuration de la base de données : DB_HOST : pg.electric-marinade.com ; DB_NAME : sni-staging ; DB_PORT : 5432 ; SERVER_NAME : backend.electric-marinade.com ; CORS_ALLOWED_ORIGINS : https://edge[.]omrisystems[.]com. https://archive.ph/rFYQU. https://archive.ph/pLRUc. https://archive.ph/F7lhK. https://www.facebook.com/photo/?fbid=10239149286493679 ; https://www.facebook.com/photo/?fbid=10239149286253673 Yigal UNNA est le premier et ancien directeur général de l’Israel National Cybersecurity Directorate (INCD), l’entité gouvernementale responsable de la structuration de la politique de cybersécurité et des capacités de cyberdéfense israéliennes. L’INCD est le résultat, en 2017, de la synthèse des compétences de plusieurs services israéliens préexistants. Numéro d’enregistrement de la société Galacticos LTD : 516599818. Checkid.co[.]il/company/GALACTICOS++LTD-OJX4GOe-516599818. https://archive.ph/39hOK ; https://archive.ph/rqkBs. https://archive.ph/7wBsK. https://archive.ph/Rs6pH et https://archive.ph/0kOnB.

<!-- page 30 -->
3.3.3. Des liens avec un business angel israélien Les recherches menées sur ces différentes entités révèlent que Blackcore fait partie d’un vaste écosystème d’entreprises et d’entités cyber israéliennes. Yigal UNNA apparaît alors comme une figure centrale de cet écosystème, d’une part en tant qu’ancien directeur général de l’Israel National Cyber Directorate (INCD) et d’autre part du fait de ses différentes expériences passées au sein de l’Unité 8200 de Tsahal et du Shin Bet 121.

Yigal UNNA est désormais à la tête d’un incubateur d’entreprises cyber israéliennes, Cygun LTD, qu’il a fondé le 10 octobre 2022 122. Le site cygun.co.il 123 présente à la fois le parcours de Yigal UNNA ainsi Photo de Yigal UNNA sur que les objectifs de l’entreprise, dont les activités reposent cygun.co.il essentiellement sur un appui dans le domaine cyber auprès d’entités gouvernementales et privées.

Logo de l’entreprise Galacticos LTD sur cygun.co.il.

Ainsi, Cygun développe des activités de conseil et divers projets via la constitution d’un réseau, notamment avec des partenaires tels que le fonds d’investissement israélien Iron Nation. Cygun participe également au soutien de nombreuses startups israéliennes, dont Galacticos AI 124. La plupart des startups affichées aux côtés de Galacticos AI comptent Yigal UNNA dans leurs instances de direction.

L’Unité 8200 constitue une branche des services de renseignement de l’armée israélienne (Tsahal), notamment spécialisée dans le renseignement d’origine électromagnétique (SIGINT). Le Shin Bet est le service de renseignement intérieur israélien. Numéro de société : 516559317 ; https://archive.ph/poaUz. https://archive.ph/a3TpX ; https://archive.ph/lXsN8. https://archive.ph/6Mw2E liste des startups soutenues par Cygun LTD sur le site de l’entreprise.

<!-- page 31 -->
4. ATTEINTE AUX INTÉRETS FONDAMENTAUX DE LA NATION

Au regard de son activité malveillante, le MOI Rokh Solis a délibérément été employé pour chercher à altérer l’information des citoyens dans le cadre du scrutin municipal, et est donc de nature à porter atteinte aux intérêts fondamentaux de la Nation.

En effet, d’une part, les actifs numériques blogdesophie.com, delogupourpalestine.com et piquemalzero.com ont visé à dénigrer le parti LFI ainsi que trois de ses candidats, à la fois sur la base de l’instrumentalisation d’éléments programmatiques, ainsi que d’accusation de crimes ou de délits.

D’autre part, les actifs numériques, dont @renaissancecatholiquefr sur TikTok, rcn-france.org, lalternative2026.com et le groupe Facebook « Forsane Alizza » se sont attachés à polariser le débat public numérique en instrumentalisant la communauté musulmane. Cependant, les différents actifs numériques du MOI Rokh Solis n'ont eu qu'une très faible visibilité en ligne une fois déployés, malgré de nombreuses tentatives d'amplification artificielle. Au regard des éléments précités et de la complexité, la longévité et de l’étendue de ce dispositif, VIGINUM considère que les activités de Rokh Solis réunissent les critères d’une ingérence numérique étrangère. Le Service poursuivra ses investigations afin de caractériser encore davantage la nature des opérateurs à l’origine du MOI Rokh Solis.

<!-- page 32 -->
5. ANNEXES

5.1. Chronologie de déploiement des campagnes du MOI Rokh Solis

> **[Figure F31 - p. 32] Chronologie (schéma, annexe 5.1)**
>
> Frise verticale centrale sur laquelle sont placés les repères de dates (cercles « 2024 », « 2025 », « 2026 » et encadrés rouges de mois). À gauche, un encadré en tirets rouges « Déploiement des sites et comptes sur les plateformes » ; à droite, un encadré « Amplification artificielle par des comptes inauthentiques ». Les icônes indiquent la plateforme (site web, TikTok, X, Facebook, Instagram).
>
> Colonne de gauche (déploiement), dans l'ordre :
> - Septembre 2024 : compte TikTok « Irelandrealnews/trueirishvoice », puis flèche pointillée « renommé » vers le compte TikTok renaissancecatholiquefr.
> - Mars 2025 : rcn-france.com, flèche « redirection » vers rcn-france.org (25/03/2025).
> - Août 2025 : comptes TikTok renaissancecatholiquefr, drole_230 et justefrancais.
> - Septembre 2025 : forsane-alizza.eu (14/09/25).
> - Octobre 2025 : pages Facebook ForsaneAlizza12 (27/10/2025), forsanealizza (2025), ForsaneAlizzafr, ForsaneAlizzafrance, et compte Instagram forsanealizza.
> - Février 2026, trois groupes : lalternative2026.com (09/02/2026) et page Facebook « Guiraud Est Zéro. Les faits que David Guiraud ne veutpas que voussachiez » (13/02/2026) ; un premier encadré gris réunissant delogupourpalestine.com (19/02/2026), la page Facebook « Qui est Sébastien Delogu? » (09/02/2026), blogdesophie.com (09/02/2026), le compte X LeBlogdeSophie0 (18/02/2025 tel qu'écrit, année probablement fautive pour 2026) et la page Facebook « Le Blog de Sophie » (19/02/2026) ; un second encadré gris avec piquemalzero.com (11/02/2026) et la page Facebook « Je suis François Piquemal et je ne suis rien. » (09/02/2026).
> - Mars 2026 : page Facebook forsanealizza0 (02/03/2026).
> - Une barre rouge horizontale : « Semaine du 10 mars 2026 : Suppression de la plupart des comptes, pages et sites du MOI ».
>
> Colonne de droite (amplification) :
> - Un encadré gris « Comptes plus anciens, mais changent tous de username entre février et aout 2025. Ils amplifient les publications du compte X @LeBlogdeSophie0 en février 2026. » listant 14 comptes X : manon_bergers, Celinejtaime, Gabholland_, its_anasantos, reevaw_, Etienne3Martin, JulienGGarcia, Adrienubois, AmeliCharpente, LeaaaLeclerc, ClairebBernard, Theo0Blanc, AlexandrMoreau, LucasgGirard.
> - Flèche « équivalent » vers un encadré bleu de 34 comptes Facebook : Julie Simon, Elise Martin, Dimitri Davin, Claire Bernard, Camille Bouvet, Alexandre Moreau, Claire Martin, Amélie Charpentier, Maxime Meireles, Thomas Germain, Adrien Dubois, Alice Thomas, Théo Blanc, Camila Savard, Jimena Quiles, Eloise Rousseau, Manon Berger, Amélie Fontaine, Sophie Dupont, Giti Yee, Céline Aimé, Olivia Reyes, Thomas Durand, Margarida Lopes, Margaux Faure, Etienne Martin, Antoine Delaunay, Adolfo Martins, Claire Perrin, Hugo Roche, Luc Dupont, Fernanda Gaspar, Enfance et Partage, Renato Vieira.
> - Flèche « équivalent » vers un encadré orange de 10 comptes Instagram : theo0blanc, julie_12simon, camilbouvet, fitness_camila, dimitri_davin, ameliefontaine, lea_6leclerc, clair3martin, sophiesodupont, jimena.quiles1.
> - Flèche descendante depuis l'encadré des comptes Facebook vers un encadré bleu de 12 groupes Facebook : Libres débats entre militants ouvriers ; Nous ne voulons + de la FRANCE en AFRIQUE ; Les Musulmans de Marseille ; Arabes à Toulouse DZ ; GILET JAUNE (Officiel) ; Militants de Droite ; info citoyenne pour demain ; GAUCHE NON POUTINISTE NON PRO HAMAS ; @Combat Républicain 2027 ; Groupe de Morannes sur Sarthe -Daumeray Participation citoyenne ; Le Canard Enchaîné Libre ; Ensembles de groupes anglophones. Légende : ensemble de groupes intégrés de manière coordonnée par les comptes inauthentiques entre septembre 2025 et février 2026, qui permet le relais des contenus publiés sur les pages du MOI Rokh Solis.
> - Quatre encadrés verts d'actions, reliés aux mois par des flèches : Octobre 2025, « Relais du hashtag #forsanealizza sur la page de Julien ODOUL et diffusion auprès des députés RN et Ensemble via WhatsApp » ; Février 2026, « Diffusion en ligne de publications faisant la promotion d'action dans la sphère physique (tract à Paris et Marseille) » et « Relais du site lalternative2026.com par @renaissancecatholiquefr sur TikTok » ; Mars 2026, « Relais du nom de domaine piquemalzero.com par des comptes inauthentiques » et « Relais de lalternative2026.com par la page Facebook Forsane Alizza ».
>
> Ce que la figure enseigne : le dispositif se construit sur deux ans. Les actifs les plus anciens (TikTok anglophone, septembre 2024 ; rcn-france, mars 2025 ; forsane-alizza.eu et ses pages, septembre-octobre 2025) précèdent de plusieurs mois la salve de février 2026 où naissent, en moins de deux semaines (9 au 19 février), les quatre sites et les pages Facebook ciblant les candidats ; les comptes amplificateurs sont préparés dès l'automne 2025 (changements de username en août 2025, intégration de groupes à partir de septembre 2025). La suppression massive de la semaine du 10 mars 2026 suit la publication du Monde du 9 mars 2026.

<!-- page 33 -->
5.2. Schéma de l’infrastructure web du MOI Rokh Solis

> **[Figure F32 - p. 33] Schéma d'infrastructure web (annexe 5.2)**
>
> Graphe d'infrastructure du MOI. Les six sites principaux sont encadrés en rouge, avec leur date d'enregistrement : rcn-france.org (25/03/2025), lalternative2026.com (09/02/2026), delogupourpalestine.com (19/02/2026), piquemalzero.com (11/02/2026), blogdesophie.com (09/02/2026) et forsane-alizza.eu (14/09/25). Des icônes distinguent domaines, serveurs DNS, adresses IP, comptes (silhouettes), images (fichiers .png), courriels et réseaux sociaux ; une icône de drapeau israélien symbolise les marqueurs d'extranéité. Les flèches ne sont pas toutes interprétables avec certitude ; seuls les liens lisibles sont listés.
>
> Nœuds et relations :
> - rcn-france.com → redirige → rcn-france.org. Le compte TikTok renaissancecatholiquefr est relié à rcn-france.com et, par « Localisation du compte », au drapeau israélien. rcn-france.org → formulaire form.jotform.com/250831579706363 → numéro +33614581823 (annoté « SFR ? »). rcn-france.org → DNS NS → penny.ns.cloudflare.com et rocky.ns.cloudflare.com. Une flèche pointillée épaisse relie rcn-france.org à lalternative2026.com.
> - lalternative2026.com → DNS NS → carol.ns.cloudflare.com et simon.ns.cloudflare.com. Lié à un « Administrateur Wordpress » « akszn ». Liés aux images « Autonomie-communautaire.png, Autorisation-du-port-de-signes-religieux.png, Autorisation-et-extension-de-lappel-a-la-priere.png, Construction-de-mosquees-et-de-minarets.png… » (annotées « Caractères hébreux dans les métadonnées », reliées au drapeau israélien) et aux images « 1-2.png, 1.png … 7.png ». Les images sont reliées à des comptes Canva (voir ci-après) et à l'icône « Gemini AI generated img (Modus Operandi) », également liée à blogdesophie.com.
> - Comptes et marques Canva (icône Canva reliée à trois utilisateurs : AmichaiNissan5, usmanlatif655 et Nawaz Akram). Premier encadré : « user »:« UAEilClIuoU », « brand displayName »:« a's team », « brandname »: amichai-nissans-team, « brand creation » : 27/06/2021, « brand »:« BAEilF2yAK0 », « locale » : en. Second encadré : « user »:« UAGV0CkkbGg », « personalBrand »:« BAGV0GgSHyY », « brand creation » : 07/11/2024, « brand »:« Nawaz Akram's Class », « locale »:« en-GB ». delogupourpalestine.com est relié au second encadré ; les fichiers 1-2.png à 7.png et « Untitled-design.png 2026/02 » sont reliés à l'auteur « usman latif » ; « Untitled-design.png » est relié au second encadré Canva et à blogdesophie.com.
> - Adresses IP et hébergement : lalternative2026.com, piquemalzero.com et blogdesophie.com pointent (flèches épaisses) vers 198.177.120.194, AS 22612, Namecheap NL ; delogupourpalestine.com pointe vers 198.177.120.196, AS 22612, Namecheap NL. blogdesophie.com → DNS NS → keaton.ns.cloudflare.com et carlane.ns.cloudflare.com. piquemalzero.com → « WHM » → blogdesophie.com, et une ligne « Web Host Manager » relie le groupe à lalternative2026.com.
> - blogdesophie.com → administrateur Wordpress « dhlkm-admin » ; → comptes « LeBlogdeSophie0 » sur Facebook et sur X, ce dernier relié par une flèche pointillée au compte X « Manon Berger / manon_bergers », lui-même relié au compte Facebook forsanealizza ; → « Screenshot-1.png » (capture de bureau) → logo OrangeWebsite → courriel blogdesophie@protonmail.com (9 February, 2026), avec un point d'interrogation « ?? » sur le lien.
> - forsane-alizza.eu → IP 80.78.18.115, AS 39287, Materialism s.r.l., SE ; → courriel forsanealizza@proton.me (14/09/2025) ; → pages Facebook forsanealizza, ForsaneAlizza12, forsanealizzafrance et Instagram forsanealizza, qui partagent la bio « Le jeu n'est pas terminé. Nous sommes là pour changer la France » ; → « Flair.png » → compte Canva « dolipart » : brand BAGy77qLSTo, branddisplayName « הצוות של Dolipart », locale he-IL, brand_creation 14/09/2025, user_id UAGy7_m2QEo, relié au drapeau israélien.
>
> Ce que la figure enseigne : les sites partagent peu d'infrastructure de façon directe (derrière Cloudflare, un même fournisseur Namecheap/AS 22612 et des serveurs de noms Cloudflare différents pour chaque site) mais sont reliés par des artefacts de fabrication : mêmes gabarits WordPress et accès WHM, mêmes images générées avec Gemini, mêmes comptes Canva (auteur usman latif, marque « Nawaz Akram's Class »), mêmes pseudonymes d'administration. Un compte Canva en locale hébreu (he-IL, créé le jour de l'enregistrement de forsane-alizza.eu) et des métadonnées hébraïques constituent les marqueurs d'extranéité.

<!-- page 34 -->
5.3. Listes des comptes inauthentiques sur Meta et des groupes ciblés

- Liste des comptes inauthentiques sur Facebook ayant ciblé les élections municipales 2026 :

Nom ID Actif Autres comptes Archive

instagram.com/the0blanc Théo Blanc 100009837391284 Oui https://archive.ph/DjYUV x.com/Theo0Blanc Julie Simon 100008980603303 Oui instagram.com/julie_12simon https://archive.ph/p1X5u Elise Martin Non Adrien Dubois Non x.com/Adrienubois https://archive.ph/UJMcP Camille Bouvet 100000966362066 Oui instagram.com/camibouvet

Romi Shushan / Maxime 100002001680594 Oui instagram.com/romishushan446 http://archive.ph/q059Z Meireles Shirel Matana / Amélie 1197189204 Oui x.com/AmeliCharpente https://archive.ph/5BYtq Charpentier Alexandre Moreau Non x.com/AlexandrMoreau Camila Savard / Todd Shaw 100084410421939 Oui instagram.com/fitness.camila https://archive.ph/2dnnW Dimitri Davin / Frederick 100005496518952 Oui instagram.com/dimitri_davin https://archive.ph/2q9AR Nikolai Julien Garcia Non x.com/JulienGGarcia Thomas Germain Non Eloise Rousseau Non Manon Berger / Forsane 100068076475246 Oui x.com/manon_bergers Alizza Céliné Aimé Non x.com/Celinejtaime Amélie Fontaine 100052425981422 Oui instagram.com/amelieeefontaine https://archive.ph/asiGO Antoine Delaunay Non Claire Bernard Non x.com/ClairebBernard Luc Dupont Non Giti Yee 61585985705539 Oui https://archive.ph/Cyhvu Etienne Martin Non Margaux Faure Non x.com/LeaaaLeclerc Lea Leclerc 100025205192191 Oui https://archive.ph/Lmd75 instagram.com/lea_6leclerc Thomas Durand Non Margarida Lopes 61585780155956 Oui https://archive.ph/aGpLB Renato Vieira 61585805924593 Oui https//archive.ph/OtNsG Adolfo Martins 61586021854055 Oui Fernanda Gaspar 61586216963960 Oui https://archive.ph/xl0pB Guiraud Est Zéro. 61586021854055 Non https://archive.ph/gx2bb Enfance et Partage 61587863747582 Oui https://archive.ph/BPnwJ Hugo Roche Non Claire Perrin Non Ngân Hale 68 100075968945688 Oui https://archive.ph/tt5rb Briceño Arteaga Steven Non Claire Martin Oui instagram.com/clair3martin https://archive.ph/iaJMF Alice Thomas 61580030243617 Oui https://archive.ph/JJsKi Olivia Reyes Non Sophie Dupont 100009534210453 Oui instagram.com/sophiesodupont https://archive.ph/bufVO Jimena Quiles 100012527208960 Oui instagram.com/jimena.quiles1 https://archive.ph/dPQsK

<!-- page 35 -->
- Liste des Groupes Facebook intégrés par les comptes cités supra :

Groupe Date d’intégration Archive

@Combat Républicain 2027 7 septembre 2025 https://archive.ph/kWMBO GILET JAUNE (Officiel) 19 et 20 février 2026 https://archive.ph/hTKra Groupe de Morannes sur Sarthes -Daumeray Participation 7 septembre 2025 https://archive.ph/MSRHC Citoyenne GAUCHE NON POUTINISTE NON PRO HAMAS 7 ou 8 septembre 2025 https://archive.ph/rS9F7 Libres débats entre militants ouvriers… 2 septembre 2025 https://archive.ph/CRGZz Info citoyenne pour demain 7 septembre 2025 https://archive.ph/pL4bD Le Canard Enchaîné Libre 7 septembre 2025 https://archive.ph/1K5sA Militants de Droite 3 et 7 septembre 2025 https://archive.ph/F6omb Nous ne voulons + de la France en AFRIQUE 19 février 2026 https://archive.ph/Q6G8J facebook.com/groups/29662 Arabes à Toulouse DZ 23 février 2026 7961925871/user/6158598570 5539 facebook.com/groups/110949 Les Musulmans de Marseille 23 février 2026 5699125734/user/6158578015 5956/ #WalkAway Campaign 8 octobre 2025 https://archive.ph/Zvi7d Brockton Political Central Octobre 2025 https://archive.ph/6bgbn Du 30 décembre 2025 au 1er https://archive.ph/9rJ05 ; Canadian Politics on Facebook janvier 2026 https://archive.ph/crCxd Bunge La Nyangores (B.L.N) 8 ocotbre 2025 https://archive.ph/naAqP https://archive.ph/5Gxnc Desi in Chicago, Illinois 30 septembre 2024

For rent in Hopkinsville/Christian county, KY 3 janvier 2025 https://archive.ph/q9dJM Cars For Sale Kentucky 3 janvier 2025 https://archive.ph/xgH0j Democracy Advocate Initiative 1er janvier 2026 https://archive.ph/X5bdZ https://archive.ph/Yl8Pf ; Gnat Youth 8 octobre 2025 https://archive.ph/bHGAS L anD in Paris France 20 janvier 2026 https://archive.ph/Thjzp Jobs in Paris (and rest of France) 20 janvier 2026 https://archive.ph/wte7M Mastrick, Sheddocksley & Summerhill Community Council 8 octobre 2025 https://archive.ph/dmQnQ World Evangelism Missions 1er novembre 2025 https://archive.ph/0VQ6X This is Woodhaven, NYC 12 octobre 2025 https://archive.ph/jzhwO Ozone Park Residents Association 8 octobre 2025 https://archive.ph/k29ou Morris Park In The Bronx 8 octobre 2025 https://archive.ph/FLTQJ https://archive.ph/Kge17 ; Grow As A Keyboardist 1er janvier 2026 https://archive.ph/pWlcb JEUNESSE EN MISSION 1er janvier 2026 https://archive.ph/DNFpy CHRETIENS DES ASSEMBLEES DE DIEU DU BURKINA FASO 1er janvier 2026 https://archive.ph/sLA9U Gwoup priyè pou Kris 1er janvier 2026 https://archive.ph/gfE1A MAISONS LAFFITE D’AVANT ET DE DEMAIN 1er janvier 2026 https://archive.ph/ymz9a

<!-- page 36 -->
- Liste des comptes inauthentiques sur Facebook et Instagram impliqués dans une opération informationnelle en Angola :

Nom ID Autres comptes Archive

Ana Costa 100065275154182 instagram.com/ana.costa71° https://archive.ph/hdH0O Ana Fernandes 61566985475011 https://archive.ph/gdk7Z Ana Silva 61578598422959 https://archive.ph/tWZYA Beatriz Sousa 61583790852561 https://archive.ph/7rEBj Carla Neves 61577987549046 https://archive.ph/yo5tJ Carlos Fernandes 61567345971248 https://archive.ph/n7qly Carlos Fernandes/Alex Dupuis 61553237953183 https://archive.ph/xnH41 Carlos Silva 100091783742493 https://archive.ph/IS6dB Claudia Gomes 61579254197392 https://archive.ph/pM21w Diogo Pereira 100091682196038 https://archive.ph/lbb6L Elda Campos 61579335971978 https://archive.ph/NWvyc Elisa Rocha 61579797809650 https://archive.ph/3CeZm Elsa Moreira 61566923018785 https://archive.ph/LC8BE Francisco Moreira 100091760943581 https://archive.ph/rzDYy Hugo Gomes 61579414906699 https://archive.ph/TsxF1 Isabel Novais 100091830510088 https://archive.ph/DFM98 Isabel Vicente 100091784311620 https://archive.ph/kg0TH Jorge Campos 61578789192522 instagram.com/jorgecamposa17 https://archive.ph/YMO0y José Machado 61566878321334 https://archive.ph/CSo7d José Silva 61578754215908 https://archive.ph/dnyvu Joseph Adu/Isabel Franco 100091734470667 https://archive.ph/leI2X Julia Martins 61579817459526 https://archive.ph/BhJ4o Julia Silva 61578979766787 https://archive.ph/qHeeA Kauan Borges 100026779241205 instagram.com/kauanborges.h76 https://archive.ph/QnORT Kiala Manuel Fernandes 61567523203140 https://archive.ph/W8bcn Kodjo Mensah/Ana Dos 61555847610347 https://archive.ph/zZCdi Santos Luisa Pereira 61578843424485 https://archive.ph/stPbC Luzia Andrade 61579389609782 Luzia Andrade | Facebook Maria Ferreira 100012961032657 instagram.com/mar.ferreira43 https://archive.ph/xDkAE Maria Sousa 61553750844339 https://archive.ph/2MG7P Mariana Costa 61579907332260 https://archive.ph/vaBlI Mariana Pereira 100091798291763 https://archive.ph/6HIEY Marta Castro 61578975732597 https://archive.ph/LpC6Y Marta Dias 61579720739286 https://archive.ph/pXiif Mateus Silva 61568633517176 https://archive.ph/yeMJm Miguel Correia 61578910990796 https://archive.ph/FJqx2 Nancy Kato 61567551098312 https://archive.ph/pLxuX Patricia Domingos 100004287882617 https://archive.ph/nbzgI Paulo Carvalho 61579038305054 https://archive.ph/LNnSn Paulo Costa 61579286237069 https://archive.ph/zcp20 Paulo Nunes 61579629507371 https://archive.ph/uy9c0 Pedro Costa 61579609774855 https://archive.ph/d0lJP Pedro Moreira 61578786035910 https://archive.ph/GNMcd Rafael Almeida 100091825800193 https://archive.ph/rA3TN Raquel Lourenço 61568282440959 https://archive.ph/S035N Rita Santos 61578854823330 https://archive.ph/tGnVb Sofia Costa 100091774052498 https://archive.ph/xfzPf Soykat Hasan/Lucas Neto 100091777472324 https://archive.ph/XksEr

<!-- page 37 -->
- Liste des Groupes Facebook intégrés par les comptes cités supra :

Groupe Date d’intégration Archive

https://archive.ph/E0spE POLITICA PARA TODOS 30 mai et 2 juin 2025 https://archive.ph/vjMe3 27 septembre 2025 ; 10, 11, 17 et 22 Angola https://archive.ph/9xDYA novembre 2025, 24 janvier 2026 Angola em Movimento 12-13 décembre 2025 https://archive.ph/stNrN https://archive.ph/Tsaol ; ANGOLA : POVOS & LINGUAS 20 mai et 2 juin 2025 https://archive.ph/xn1S3 https://archive.ph/4jZng ANGOLA. O PAIS 25 mai 2025

https://archive.ph/IRi0G 20 mai 2025 ; 13 décembre 2025 ; https://archive.ph/WpT9H ANGOLANOS UNIDOS POR UMA ANGOLA MELHOR 25 janvier 2026 ; 1er et 6 février https://archive.ph/tlRDU 2026 https://archive.ph/rlutX http://archive.ph/dCD4d DENUNCIA PUBLICA ANGOLA 31 mai et 3 juin 2025 https://archive.ph/GnXvE INFORMAR COM VERDADE 2 novembre 2025 https://archive.ph/0suub https://archive.ph/wHhk3 https://archive.ph/beAYt 17, 18, 22 et 23 novembre 2025 ; 24 MPLA – CUBANGO https://archive.ph/huy2D et 25 janvier 2026 https://archive.ph/LixuL https://archive.ph/e6vWT https://archive.ph/hbPYh MPLA – FACEBOOK DO CAP DE LISBOA 2, 18, 23 et 24 novembre 2025 https://archive.ph/8xOOn https://archive.ph/YQ8DM 2, 17, 18, 22, 23 et 29 novembre https://archive.ph/fQrTe MPLA RUMA A 2027 E A VITORIA E CERTA 2025 ; 24 et 26 janvier 2026 https://archive.ph/j05V0 https://archive.ph/UdaH4 https://archive.ph/zM9mb Ordem dos amigos contabilistas de Angola 31 mai et 3 juin 2024 https://archive.ph/xHPkN OS JURISTAS E ESTUDIANTES DO CURSO DE 2 novembre 2025 https://archive.ph/dhNYU DIREITO/INTERACCAO TOTAL https://archive.ph/W2qFN 26 novembre 2025 ; 13 et 14 https://archive.ph/0apHS VIANA= MPLA,OMA,JMPLA « MELHORAR O QUE ESTA décembre 2025 ; 25 et 26 janvier https://archive.ph/2fJJg BEM, CORRIGIR O QUE ESTA MAL » 2026 ; 2 et 12 février 2026 https://archive.ph/5nILl https://archive.ph/ISnn4 https://archive.ph/L0mp9 20, 22 et 26 novembre 2025 ; 25 https://archive.ph/pu02S VOZ DA DIASPORA janvier 2026 https://archive.ph/2ZFFB https://archive.ph/wND7J As Mulheres Mais Lindas de Angola 11 septembre 2025 https://archive.ph/oqXQL Dr Abel Chivukuvuku Para Presidente em 2027. EU APOIO ! 8 novembre 2025 https://archive.ph/SeWiT African History Group 5 janvier 2026 https://archive.ph/rgdOu Трансерфинг реальности 26 août 2025 https://archive.ph/agUy2

<!-- page 38 -->
5.4. Liste du cluster de comptes X impliqués dans la campagne informationnelle visant les élections municipales

Commente Date de Partage Agir la Date de modification Suit le blog Localisation du Version Compte Ensemble publication Création du nom de Sophie compte X et/ou ELNET de Sud d’utilisateur Radio Etienne3Martin Avril 2019 Août 2025 X Royaume-Uni Web Octobre Royaume- JulienGGarcia Août 2025 X Web 2012 Uni EscofilPericles Juin 2014 Inconnue X X X Europe Web Septembre Adrienubois Août 2025 X X Europe Web 2015 AlexandrMoreau Mars 2022 Août 2025 X X Europe Web Février manon_bergers Février 2025 X X Europe Web 2012 Theo0Blanc Mars 2014 Août 2025 X X Europe Web Février AmeliCharpente Août 2025 X X Royaume-Uni Web 2013 Janvier LeaaaLeclerc Août 2025 X X Europe Web 2015 Septembre Celinejtaime Février 2025 X X Europe Web 2012 Janvier App LucasgGirard Août 2025 X Suisse 2014 Store ClairebBernard Mars 2015 Août 2025 X Europe Web Gabholland_ Mai 2025 Mai 2025 X Europe Web its_anasantos Mai 2025 Mai 2025 X Europe Web reevaw_ Mai 2025 Mai 2025 X Europe Web ErnestEnchelom Août 2025 Août 2025 X France Web

<!-- page 39 -->
5.5. Schéma de l’infrastructure de Blackcore

> **[Figure F33 - p. 39] Schéma d'infrastructure et de relations entre entités (annexe 5.5)**
>
> Graphe de l'infrastructure de Blackcore et de l'écosystème d'entités israéliennes associé. Les encadrés colorés regroupent les sous-domaines : bleu clair (omrisystems.com), orange (electric-marinade.com), vert (blackcore.online), vert clair (iron-mind.ai / nirostools.com), violet pointillé (galacticos.ai, titres « Avatar Data Generator by Galacticos AI ») ; les éléments en rouge relèvent des opérations Sadaqah Palestine et Angola. Une partie des flèches n'est pas lisible avec certitude ; seules les relations visibles sont listées.
>
> Infrastructure web :
> - IP 94.237.57.56, AS 202053, UpCloud Ltd ; GB : cible de omrisystems.com (12/02/2026), electric-marinade.com (19/02/2025) et proposals.blackcore.online.
> - blackcore.online (22/08/2025) → proposals.blackcore.online ; → serveurs de noms lindsey.ns.cloudflare.com, jakub.ns.cloudflare.com, brenda.ns.cloudflare.com, luke.ns.cloudflare.com ; → entité « Blackcore » → compte LinkedIn « Blackcore » → « Publication LinkedIn ».
> - Sous-domaines de omrisystems.com : www, agentforge, console, socialtrigger, edge, flowise, avatar-data-generator, fb-search.
> - Sous-domaines de electric-marinade.com : autodiscover, gologin, backend, fb-search, sni-g, omri, avatar-data-generator ; serveur de messagerie smtp.electric-marinade.com ; sous-domaines « fb.aoa.omrisystems.com » et « fb.aoa.electric-marinade.com » dont les « sous-domaines correspondants » mènent à fb.aoa.modules.galacticos.ai.
> - Sous-domaines de blackcore.online : landing, ava, angola-plan (flèche pointillée vers angola-plan.pages.dev), demo, www, en, auth, ftp, app, ns1, ns2.
> - Titre de site « Login - Avatar Data Generator by Galacticos AI », porté par les avatar-data-generator d'omrisystems.com et d'electric-marinade.com ; lié à avatar-data-generator.dev.iron-mind.ai. Autres sous-domaines : network-search.galacticos.dev.iron-mind.ai, omri.dev.iron-mind.ai, instasearch.dev.iron-mind.ai, omri-dev.nirostools.com, ironmind.nirostools.com, hébergés sur 172.238.242.191, AS 63949, Akamai Connected Cloud, SE. iron-mind.ai (01/10/2025) et nirostools.com (01/10/2024) y sont rattachés.
> - galacticos.ai (17/04/2024) → IP 5.199.173.246, AS 16125, UAB Cherry Servers, LT, qui héberge aussi sni.ai (31/12/2021). Guy Geyor est enregistré comme « Registrant » de galacticos.ai et est lié à sni.ai et à la société SNI.
>
> Entités et personnes :
> - Galacticos LTD, Cygun LTD (« Promu par » via cygun.co.il), Afik & Co., SNI : toutes rattachées à l'adresse « 103 Hachashmonaim, Tel Aviv, Israel ».
> - Yigal Unna → Cygun LTD et Galacticos LTD ; Doron Afik → Afik & Co., Galacticos LTD, SNI, afiklaw.com, compte LinkedIn Afik & CO. et un groupe de comptes Instagram ; Guy Geyor → SNI et le même groupe Instagram.
> - Nir Dobicky → iron-mind.ai ; → Iron Mind AB, Rocketpod Technologies LTD, Blade Marketing / Social Worx LTD ; → LinkedIn « Niro D. Knox » qui « Interagit avec » le compte LinkedIn Afik & CO. ; → compte X Niroknox ; → le groupe Instagram.
> - Deux « Publications LinkedIn » (Blackcore et Afik & CO.) liées à des « Interactions produites par des comptes inauthentiques similaires ».
>
> Opérations Sadaqah Palestine / Angola (en rouge) :
> - sadaqahpalestine.com (12/02/2025) → compte X SadaqahPalestin → logo SADAQAH PALESTINE ; sadaqahpalestine.com est « lié à » smtp.electric-marinade.com (tirets orange) ; → numéro +972-549729306 → TikTok deepdiveafrica → « Localisation du compte » : Israël ; → logo « Politizando Angola » → Instagram politilizando_angola et Facebook Politizando Angola → « Amplification des contenus dans des groupes Facebook par des cluster de comptes similaires », alimentée aussi par les pages Facebook « Je suis François Piquemal et je ne suis rien », « Guiraud Est Zéro. Les faits que David Guiraud ne veutpas que voussachiez », « Qui est Sébastien Delogu? » et « Le Blog de Sophie ».
> - paullussac (Fiverr) → logo SADAQAH PALESTINE et → twostepinnovations.com → IP 185.230.63.107, 185.230.63.171, 185.230.63.186 et 34.149.87.45, reliées au domaine blackcore.online.
> - « Interactions produites par des comptes inauthentiques similaires » → SadaqahPalestin et → Niroknox (flèches pointillées rouges).
>
> Ce que la figure enseigne : c'est la preuve visuelle du chaînage décrit en 3.3. Blackcore (pseudo-société sans statut légal, domaine blackcore.online enregistré le 22 août 2025, avec un sous-domaine angola-plan) partage un serveur et des outils (AI Facebook Group Finder, générateur d'avatars) avec des sociétés israéliennes de l'automatisation d'influence (Galacticos AI, SNI, Iron Mind, Rocketpod, Social Worx), toutes reliées à un même cercle de personnes (Yigal Unna, Doron Afik, Guy Geyor, Nir Dobicky) et à une même adresse à Tel Aviv. Le rapport rattache à cette infrastructure les opérations Sadaqah Palestine (comptes amplificateurs visant aussi les comptes écossais SNP et John SWINNEY), Angola et municipales françaises. Le rapport précise que l'étendue de cet écosystème ne permet pas de qualifier le rôle exact des opérateurs à l'origine de Rokh Solis ni l'identité des commanditaires.

<!-- page 40 -->
5.6. Liste du cluster de comptes LinkedIn impliqués dans l’amplification de la publication de Blackcore

Réaction à la Réaction à la Nombre Nom publication de Localisation publication d'Afik & CO. d'abonnés Blackcore Laurie Tang oui oui Singapour 3 353 Mike Thest oui oui Cebu, Philippines 588 Mario Giacobo oui oui Madrid, Espagne 3 275 Vanessa Valkova oui oui New York, États-Unis 8 621 José Luis Rivero oui oui Madrid, Espagne 10 047 Pablo Mark oui oui Buenos Aires, Argentine 2 197 Joseph Robinson oui non New York, États-Unis 12 706 Ellen Smith oui oui San Francisco, États-Unis 556 Martina A. oui oui Padoue, Italie 27 651 Ted Wijnen oui oui Utrecht, Pays-Bas 2 606 Silvia Borralho oui oui Lisbonne, Portugal 5 893 Beny Dorsey oui non Ecosse Catherine Wiesel oui oui Torcy, France 1 168 Milena Mineo oui oui Milan, Italie 814 Licinio Gaspar oui oui Lisbonne, Portugal 3 233 Jordan Daley oui oui Exton, États-Unis 1 817 Miguel Duarte Pereira oui oui Porto, Portugal 3 277 Rich Odonnel oui oui Spring, États-Unis 2 216 Ronald Thomas oui oui New York, États-Unis 516 Sander van den Berg oui oui Amsterdam, Pays-Bas 8 092 Maria José Bravo oui oui Montijo, Portugal 7 048 Adriana Minotto oui oui Milan, Italie 4 056 Jon Tavarez oui oui Tampa Bay, États-Unis 10 366 Herman Boontje oui oui Rotterdam, Pays-Bas 757 Carla Valério oui oui Lisbonne, Portugal 4 479 Konstantin Frolov oui non Lituanie 8 606 Andrea Gatti oui oui Milan, Italie 4 376 Ruben De Beus oui oui Ede, Pays-Bas 1 246 David HTL oui non Paris, France 521 Marco Valente oui oui Montijo, Portugal 2 538

<!-- page 41 -->
À PROPOS DE VIGINUM

Créé le 13 juillet 2021 et rattaché au SGDSN, le service de vigilance et de protection contre les ingérences numériques étrangères (VIGINUM) a pour raison d’être la protection du débat public numérique touchant aux intérêts fondamentaux de la Nation.

Ce service technique et opérationnel de l’État a pour mission de détecter et caractériser les campagnes de manipulation de l’information sur les plateformes numériques, impliquant des acteurs étrangers dans le but de nuire à la France et à ses intérêts.

Service de vigilance et protection contre les ingérences numériques étrangères | SGDSN

Crédit photo couverture : B3N3 Photography - stock.adobe.com.
