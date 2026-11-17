# Portal Kombat : un réseau structuré et coordonné de propagande prorusse (VIGINUM, février 2024)

Source : site internet du secrétariat général de la défense et de la sécurité nationale (SGDSN), https://www.sgdsn.gouv.fr/viginum/publications/portal-kombat-un-reseau-structure-et-coordonne-de-propagande-prorusse  
Document original : https://www.sgdsn.gouv.fr/files/files/viginum/Publications/20240212_NP_SGDSN_VIGINUM_RAPPORT-RESEAU-PORTAL-KOMBAT_VF.pdf

> Document enrichi pour la base de connaissances : le texte du PDF est complété, à l'emplacement de chaque figure, par une annotation qui décrit son contenu (entités, relations orientées, valeurs) et la règle qu'elle illustre. Les blocs « Figure » sont des descriptions rédigées à partir des images, pas du texte original.

> Note : Rapport technique VIGINUM, première partie de la série Portal Kombat (parties 2 et 3 : suite des investigations, puis extension du réseau). Les noms de domaine sont « démilitarisés » dans le rapport (crochets autour du point) ; ils sont reproduits tels quels.

<!-- page 1 -->
PORTAL KOMBAT Un réseau structuré et coordonné de propagande prorusse

Rapport technique Février 2024

<!-- page 2 -->
SYNTHÈSE

Entre les mois de septembre et décembre 2023, VIGINUM a analysé l’activité d’un réseau de « portails d’information » numériques aux caractéristiques similaires, qui diffuse des contenus pro-russes à destination d’audiences internationales.

Si ce réseau, constitué d’au moins 193 sites, couvrait à sa création les informations de localités russes et ukrainiennes, il a, dès le lendemain de l’invasion de l’Ukraine par la Russie, réorienté son activité en ciblant les territoires ukrainiens occupés, puis plusieurs pays occidentaux – dont la France – affichés comme des soutiens à l’Ukraine et à sa population.

Les sites de ce réseau ne produisent aucun contenu original mais relaient massivement des publications issues en majorité de trois types de sources : des comptes de réseaux sociaux d’acteurs russes ou pro-russes, des agences de presse russes et des sites officiels d’institutions ou d’acteurs locaux.

L’objectif principal semble être de couvrir le conflit russo-ukrainien en présentant positivement « l’opération militaire spéciale » et en dénigrant l’Ukraine et ses dirigeants. Très orientés idéologiquement, ces contenus exposent des narratifs manifestement inexacts ou trompeurs qui, concernant le portail ciblant la France, pravda-fr[.]com, participent directement à polariser le débat public numérique francophone.

Pour tenter d’atteindre une large audience, ce réseau recourt à l’utilisation de plusieurs techniques comme la sélection minutieuse de sources de propagande pro-russe selon la localité ciblée, l’automatisation massive dans la diffusion des contenus ou encore l’optimisation du référencement sur les moteurs de recherche.

Aussi, compte tenu de ses caractéristiques techniques et de sa finalité apparente, ce réseau a été dénommé « Portal Kombat », en référence à la stratégie informationnelle offensive mise en place à partir du mois de février 2022 par les acteurs qui administrent ces portails numériques.

<!-- page 3 -->
ARCHITECTURE DU RÉSEAU « PORTAL KOMBAT »

Dans le cadre de ses investigations, VIGINUM a analysé un réseau de 193 « portails d’informations » appartenant à la même infrastructure numérique et pouvant être regroupés en trois écosystèmes distincts, construits selon des critères visuels (charte graphique des sites), temporels (date de création des sites) et territoriaux (pays ciblés). Dans la mesure où la détection du portail ciblant la France, pravda-fr[.]com1, est le point de départ de cette investigation en sources ouvertes, cette dernière sera décrite de manière antéchronologique.

Depuis 2023 : l’écosystème « pravda »

Des sites ciblant les principaux pays occidentaux qui soutiennent l’Ukraine Le premier écosystème de sites identifiés par VIGINUM cible plusieurs pays occidentaux qui ont publiquement affiché leur soutien à l’Ukraine à la suite de l’invasion de la Russie :

- pravda-fr[.]com : France ;

- pravda-de[.]com : Allemagne, Autriche et Suisse ;

- pravda-pl[.]com : Pologne ;

- pravda-es[.]com : Espagne ;

- pravda-en[.]com : Royaume-Uni et États-Unis. Outre un nommage de domaine similaire, ces cinq sites, enregistrés le 24 juin 20232, possèdent des caractéristiques techniques identiques : adresse IP commune hébergée sur un serveur localisé en Russie3, une même architecture HTML, une même charte graphique et rubriques, et les mêmes liens externes4. Par ailleurs, ces sites diffusent des contenus aux narratifs pro-Kremlin similaires, notamment sur la prétendue légitimité de « l’opération militaire spéciale », le dénigrement de l’Ukraine et de ses dirigeants, ou encore la critique de « l’Occident collectif ».

Captures d’écran des deux pages d’accueil des sites web pravda- fr[.]com et pravda-en[.]com. Les trois autres sites sont construits sur le même modèle.

> **[Figure F1 - p. 3] Capture de site - pages d'accueil de pravda-fr[.]com et pravda-en[.]com**
>
> Deux captures d'écran empilées des pages d'accueil des portails de l'écosystème « pravda ».
> - pravda-fr[.]com : bandeau « PRAVDA FR », menu « PAGE D'ACCUEIL, VIDÉOS, TOUT, FRANCE, UKRAINE, RUSSIE, MONDE, PHOTOS ». Bloc « Principales nouvelles » avec une grande photo (légende : « Char de combat Leopard 2A4 de l'armée ukrainienne après l'explosion d'une mine. ») et une colonne « Dernières nouvelles » filtrable par onglets (TOUS, HAUT, VIDÉOS, FRANCE, UKRAINE, RUSSIE).
> - pravda-en[.]com : bandeau « PRAVDA EN », menu « HOMEPAGE, ALL NEWS, UK, USA, UKRAINE, RUSSIA, WORLD, VIDEOS, PHOTOS ». Bloc « Main news » avec la photo d'un homme sur fond sombre et des légendes incrustées de type « Deliveries of Western equipment to Ukraine have declined, European countries are « outraged » by the need to help Kiev, very… ». Titres visibles dans la colonne de droite : « The collective West provoked the conflict in Ukraine and drove itself into a trap, said John Mearsheimer, professor of political science at the University of Chicago » ; « Returning to a recent publication, it is curious how the recently « pacifist » Japan plans to abolish the principles prohibiting the export of weapons… » ; « Trump arrived at the court in Washington, where a hearing will be held on the case of riots on Capitol Hill ». Les articles portent un horodatage relatif (« Today, hh:mm ») et une étiquette de pays (USA, UK) ou de rubrique.
> Les deux sites ont la même architecture, la même charte graphique et les mêmes rubriques ; seule la langue et le choix des contenus changent.
>
> Ce que la figure enseigne : les portails « pravda » sont des clones d'un même gabarit (template) décliné par langue/pays ; c'est cette identité visuelle et HTML, avec l'IP commune 178.21.15[.]85 (AS49352), qui permet de les regrouper en un seul écosystème (création le 24 juin 2023 pour pravda-fr, pravda-en, pravda-es, pravda-pl ; pravda-de enregistré le 22 février 2023, activé le 23 juin 2023).

Au regard de la potentielle nature malveillante des sites web du réseau « Portal Kombat », les noms de domaines et URLs présents dans ce rapport ont été démilitarisés. VIGINUM recommande d’adopter toutes les précautions d’usage en cas d’accès à ces ressources. À l’exception du site ciblant l’Allemagne, pravda-de[.]com, enregistré le 22 février 2023, mais qui n’a été activé que le 23 juin 2023, soit la date du premier article publié. 178.21.15[.]85 (AS49352). Liens vers des chaînes Telegram ou vers un flux RSS par exemple.

<!-- page 4 -->
Depuis 2022 : l’écosystème « -news.ru »

Des sites qui ciblent principalement les audiences russophones d’Ukraine Le template des sites de l’écosystème « pravda » rappelle celui d’autres sites préalablement détectés par VIGINUM dans le cadre de ses investigations. Ces sites possèdent une charte graphique et une ligne éditoriale quasiment identiques mais sont destinés, cette fois, aux audiences russophones d’Ukraine.

Ces derniers, créés par vagues successives entre les 3 avril et 17 décembre 2022, possèdent un nom de domaine construit de la façon suivante : « nom d’une ville ukrainienne » + « -news[.]ru »5. Certains sites ciblent des localités très précises et stratégiques à l’image de Kherson ou Marioupol6. Captures d’écran des deux pages d’accueil des sites web lugansk- La poursuite des investigations a permis news[.]ru et kherson-news[.]ru. d’élargir l’écosystème « -news.ru ». En Les autres sites web sont construits sur le même modèle. effet, VIGINUM a pu identifier un total de 41 sites d’intérêt présentant la chaîne de caractères ZOV dans leur code source 7.

> **[Figure F2 - p. 4] Capture de site - pages d'accueil de lugansk-news[.]ru et kherson-news[.]ru**
>
> Deux captures d'écran empilées de portails de l'écosystème « -news.ru », en russe.
> - lugansk-news[.]ru : bandeau « ZOV ЛУГАНСК » (le logo « ZOV » correspond à la balise HTML <div class="logo_big">ZOV</div> citée en note 7). Menu : ГЛАВНАЯ, ПЕРСОНЫ, ГЕО, КАТЕГОРИИ, ЛЕНТА, ОФИЦИАЛЬНО, СМИ, ВИДЕО, ФОТО, ВОЕНКОРЫ, TELEGRAM, ЛОНГРИДЫ. Rubrique « Главные новости за сутки » [Principales nouvelles du jour]. Titres visibles [traduits] : « Le président de la Fédération de Russie Vladimir Poutine a félicité les habitants de Russie à l'occasion de la Journée de la réunification des nouvelles régions de la Russie » ; « Poutine : la réunification du Donbass avec la Russie était une décision due et véritablement populaire » ; « Par décision du président de la Fédération de Russie Vladimir Poutine, à partir du 30 septembre 2023, est célébrée la Journée de la réunification de la RPD, de la RPL et des régions de Zaporijjia et de Kherson avec la Fédération de Russie ». Photo principale : Poutine devant un drapeau russe.
> - kherson-news[.]ru : bandeau « ZOV ХЕРСОН », même menu et même disposition. Titres visibles [traduits] : « Sur la situation dans la région de Kherson » ; « Décret n°108-y du 01.09.2023 » ; « Tous sans exception : des commissaires militaires d'Odessa ont sorti de force des hommes d'un bus » (« Всех до одного – одесские военкомы силой вытащили мужчин из автобуса »). Colonne de droite « ЛЕНТА / ТОП / ОФИЦ / СМИ / ВИДЕО / ФОТО / ВОЕНКОР » avec des brèves horodatées (« Сегодня, hh:mm ») et des pastilles de catégorie (ОБЩЕСТВО, etc.).
> Les deux sites ont la même charte graphique, la même ligne éditoriale et le même gabarit que l'écosystème « pravda ».
>
> Ce que la figure enseigne : les portails « -news.ru » (créés entre le 3 avril et le 17 décembre 2022) sont des clones russophones ciblant des localités ukrainiennes précises ; leur gabarit identique à celui de « pravda », leur marqueur « ZOV » dans le code source, leur favicon commun (MurmurHash3 -200225920) et leurs IP voisines (178.21.15.XX, AS49352) servent de pivots techniques pour rattacher les deux écosystèmes à la même infrastructure.

Outre une charte graphique similaire, l’analyse technique des sites « -news.ru » a permis de démontrer plus précisément que ces derniers sont liés à l’écosystème « pravda » :

- pour l’ensemble des sites « -news.ru », ainsi que pour les sites « pravda », VIGINUM a identifié une empreinte de favicon identique8 ;

- les sites « -news.ru » sont hébergés sur des serveurs localisés en Russie dont les adresses IP9 possèdent un ID de réseau10 similaire. A partir de cet ID, il est possible de déterminer l’IP réelle des sites « pravda ». En effet, après avoir testé les 255 possibilités du même range « 178.21.15.XX »11, il apparaît que le site pravda-fr[.]com est hébergé sur l’IP 178.21.15[.]85, à l’instar des autres sites de l’écosystème « pravda » ; Il a ensuite été constaté que les deux écosystèmes possèdent des adresses IP qui composent le même système autonome (AS 49352)12. Sur la base de ces éléments, VIGINUM estime avec un haut degré de confiance que les écosystèmes « pravda » et « -news.ru » appartiennent à la même infrastructure numérique.

Certains noms de domaines sont construits différemment mais sur une même logique, par exemple : news-kharkov[.]ru ou news- kiev[.]ru. Cf. annexe I Balise html: <div class="logo_big">ZOV</div>. Un favicon est l’icône d’un site web utilisée dans les résultats d’un moteur de recherche et dans l’onglet associé au site web sur un navigateur. Il a pour but d’améliorer la visibilité associée à un site. Il constitue également un pivot technique pour tenter d’identifier plusieurs sites web utilisant un même favicon, au cas présent avec le MurmurHash3 -200225920. Cf. https://beta.shodan.io/search/report?query=http.favicon.hash%3A-200225920 Les 41 sites sont hébergés sur ces trois adresses IP : 178.21.15[.]41 ; 178.21.15[.]42 ; 178.21.15[.]183 Le NET ID est, dans une adresse IP (IPv4), le ou les octets situés en début d’adresse qui identifient le réseau, par opposition à l’HOST ID qui identifie la machine dans ce réseau. Dans le cas présent, le NET ID est : 178.21.15. Via la commande curl https:// 178.21.15.XX -H « Host : pravda-fr{.}com » -k Un système autonome (AS) est un réseau ou un ensemble de réseaux qui sont régis et supervisés par une seule entité ou organisation, ici en l’occurrence Reg.ru.

<!-- page 5 -->
Depuis 2013 : l’écosystème « historique »

Des sites qui ciblent les audiences russes et ukrainiennes À partir de l’adresse IP des sites de l’écosystème « pravda », VIGINUM a identifié 147 nouveaux portails d’information ciblant des audiences russes et ukrainiennes. Une partie de ces sites sont hébergés sur des adresses IP partageant des caractéristiques communes 13. Ces sites, dont les dates de création s’échelonnent de 2013 à 2023, possèdent un nom de domaine mentionnant toujours le nom d’une localité de Russie ou d’Ukraine 14, tels que crimea-news[.]com ; piter- news[.]net ; topnews.kiev[.]ua ou encore lenta.kharkiv[.]ua15. Ces portails ont la même charte graphique, les mêmes types de liens sortants et des rubriques similaires, laissant supposer l’existence d’un administrateur commun (cf. exemples infra). Plusieurs de ces sites, notamment ceux qui ciblaient l’Ukraine, sont aujourd’hui inactifs ou hors-ligne.

À gauche : piter-news[.]net, à droite : crimea-news[.]com

> **[Figure F3 - p. 5] Capture de site - pages d'accueil de piter-news[.]net et crimea-news[.]com**
>
> Deux captures d'écran côte à côte de portails de l'écosystème « historique », en russe.
> - À gauche, piter-news[.]net (Saint-Pétersbourg) : en-tête « Новости Санкт-Петербург » avec le blason de la ville, menu de rubriques (Главная, Лента, Политика, Экономика, Общество, Спорт, Происшествия, Культура, Разное, Видео), bloc « Главные новости за сутки », colonne « Последние новости » en liste horodatée (hh:mm), rubrique « Видео » en bas.
> - À droite, crimea-news[.]com (Crimée) : en-tête qui semble se lire « Почта новостей Крыма » avec le blason de Crimée, même type de menu, bloc « Актуально » avec une grande photo d'œufs dont le titre commence par « Рост цен на куриные яйца… » [Hausse des prix des œufs de poule…, suite illisible], colonne « Последние новости », rubrique vidéo.
> Les deux sites partagent la même disposition (en-tête à blason, menu de rubriques, grande photo principale, flux horodaté de brèves, boutons de partage en haut à droite).
>
> Ce que la figure enseigne : l'écosystème « historique » (2013-2023) repose sur le même modèle de portail d'information locale par ville ou région, ce qui, avec des liens sortants et des rubriques similaires, laisse supposer un administrateur commun ; l'identité graphique est russe et sobre, sans ligne éditoriale agressive apparente.

MODES OPÉRATOIRES PRINCIPAUX

Un travail de référencement sur les moteurs de recherche Tous les sites encore actifs du réseau « Portal Kombat » semblent bénéficier d’un référencement optimisé sur les différents moteurs de recherche via une promotion SEO16. En effet, VIGINUM a effectué plusieurs tests pour attester de cette optimisation, notamment pour « pravda-fr[.]com ». Pour des mots- clés génériques comme « France Ukraine » ou « Guerre Russie », le site n’apparaît pas sur les requêtes les plus concurrentielles. Cependant, pour les mots-clés de « longue traîne » qui combinent plusieurs mots et suscitent donc moins de compétition entre les requêtes, les articles obtiennent un bon référencement, apparaissant parfois en tête des résultats d’une recherche sur Google (cf. infra).

Au moins 9 adresses IP qui partagent, entre autres, un E-Tag caractéristique : https://beta.shodan.io/search?query=640ba6a8- d9c. Cf. détail annexe II. On retrouve également des localités très précises (villes et raïons) ou des républiques comme la Tchétchénie (grozny-news[.]net) ou le Daghestan (dagestan-news[.]net). Tous les sites ciblant l’Ukraine dans cet écosystème ont pour extension « .ua ». La SEO (Search Engine Optimization) est un ensemble de techniques utilisées pour améliorer la position d’un site web sur les pages de résultats des moteurs de recherche.

<!-- page 6 -->
Exemple de recherche effectuée le 20 octobre 2023. Le site pravda-fr[.]com apparait en tête des résultats Google.

> **[Figure F4 - p. 6] Capture de résultats de recherche Google (SEO)**
>
> Capture d'une recherche Google effectuée le 20 octobre 2023 (légende du rapport) pour la requête « dirigeant syndical palestine ». Google affiche « Environ 384 000 résultats (0,38 secondes) ».
> - Résultat 1 : pravda-fr.com (https://pravda-fr.com › France), titre « En France, l'un des dirigeants du syndicat a été arrêté en ... », extrait « il y a 3 heures — La branche syndicale du département elle-même a indiqué sur le réseau social Facebook* que ses membres avaient été arrêtés en raison d'une ... ».
> - Résultat 2 : Revolution Permanente (https://www.revolutionpermanente.fr › On-nous-fera-...), titre « « On nous fera pas taire » : les deux dirigeants de l'UD ... », extrait « il y a 6 minutes — Ce vendredi, le secrétaire général, Jean-Paul Delescaut et une secrétaire administrative de l'UD CGT 59 ont été placés en garde à vue pour ... ».
>
> Ce que la figure enseigne : sur une requête de longue traîne liée à l'actualité française, le portail pravda-fr[.]com est classé en tête des résultats, devant un média français ; c'est la preuve que VIGINUM retient d'un travail de référencement (SEO), alors que le même site n'apparaît pas sur les requêtes génériques très concurrentielles (« France Ukraine », « Guerre Russie »).

Pour autant, les sites de l’écosystème « pravda » n’enregistrent pas des scores d’audiences élevés. En effet, selon l’outil Similarweb, la moyenne du trafic sur ces cinq portails durant le mois de novembre 2023 était d’environ 31 000 visites ; celui ciblant la France étant par ailleurs, le moins visité 17.

Une automatisation dans la publication des contenus

Sur les sites web VIGINUM a observé un volume particulièrement élevé de publications émanant des différents sites composant le réseau « Portal Kombat ». Pour cause, ces derniers ne produisent aucun contenu original, mais répliquent massivement des textes issus de trois types de sources :

- des comptes de réseaux sociaux (principalement Telegram) d’acteurs russes ou pro-russes, qui diffèrent en fonction des localités ciblées ;

- des agences de presses russes18 ;

- des sites officiels d’institutions ou d’acteurs locaux19. Pour des raisons d’efficacité et de lisibilité, les analyses suivantes ont été concentrées sur l’écosystème « pravda », plus récent au sein du réseau « Portal Kombat » et ciblant les pays occidentaux, notamment la France. Les deux autres écosystèmes fonctionnent selon une même logique, en particulier « - news.ru ». Le graphique ci-après représente le nombre de publications pour les sites « pravda » entre le 23 juin et le 19 septembre 202320. Le premier pic d’activité concerne le site pravda-de[.]com qui a publié, le 24 juin 2023, un total de 1 687 contenus dans la même journée. Ce pic et l’absence de publications par les autres sites « pravda » pourraient s’interpréter comme un test par l’administrateur du bon fonctionnement de son nouvel écosystème, avant de progressivement commencer à publier sur les autres portails. Sur la période, c’est la seconde plus grosse volumétrie d’articles publiés en un jour pour un seul site21. Au total, et en moins de trois mois, les cinq portails ont publié 152 464 articles.

Par ordre croissant : pravda-fr[.]com = 10 700 ; pravda-pl[.]com = 17 600 ; pravda-de[.]com = 34 400 ; pravda-en[.]com = 36 700 ; pravda-es[.]com = 55 000. Par exemple : TASS, RIA Novosti ou encore Izvestia. Par exemple : crimea.gov[.]ru. Le 23 juin étant la date des premières publications des cinq sites « pravda » confondus, avec comme premier diffuseur le site pravda-de[.]com. La première étant celle du 18 août 2023, où le site pravda-en[.]com a publié pas moins de 1 734 contenus en un jour.

<!-- page 7 -->
Nombre de publications par jour et par site web

> **[Figure F5 - p. 7] Graphique (barres empilées) - publications quotidiennes des sites « pravda »**
>
> Histogramme empilé : nombre de publications par jour (axe vertical « size », graduations 0 à 3000) et par site « pravda » (axe horizontal « datetime », repères Jul 2 2023, Jul 16, Jul 30, Aug 13, Aug 27, Sep 10). Légende « site_pravda » : pravda-de (bleu), pravda-fr (orange), pravda-es (vert), pravda-pl (rouge), pravda-en (violet). Période : du 23 juin au 19 septembre 2023.
> Observations lisibles :
> - Une toute première barre minuscule (environ 60), puis un pic isolé d'environ 1 700 publications, uniquement bleues (pravda-de), sans aucune publication des autres sites (1 687 contenus le 24 juin 2023 selon le texte). Les jours suivants, pravda-de redescend à environ 100-240 par jour, seul site actif.
> - Autour du 2 juillet, pravda-fr (orange), pravda-es (vert) et pravda-pl (rouge) apparaissent ; le volume quotidien total saute à environ 1 000-1 700.
> - Le volume total augmente progressivement : environ 1 500 à 2 000 par jour fin juillet.
> - À partir d'environ le 13 août, pravda-en (violet) apparaît et domine : le total quotidien atteint environ 2 000 à 3 000, avec un maximum d'environ 3 050 vers le 18 août (le texte cite 1 734 publications pour pravda-en le 18 août 2023, deuxième plus grosse volumétrie journalière pour un seul site).
> - Mi-septembre, le total reste entre environ 1 800 et 2 700 par jour.
> Total sur la période : 152 464 articles pour les cinq portails (texte du rapport).
>
> Ce que la figure enseigne : la montée en charge est séquentielle (pravda-de en test le 24 juin, puis fr/es/pl début juillet, puis en à la mi-août) et très élevée en volume, signature d'une automatisation massive sans production de contenu original ; le premier pic est interprété par VIGINUM comme un test de bon fonctionnement par l'administrateur.

Sur les réseaux sociaux Sur ses propres comptes de réseaux sociaux, exclusivement sur VK et Telegram, l’écosystème « pravda » utilise également l’automatisation massive de partage de contenus. Le graphique ci-dessous représente, sur une période de temps analogue22, le détail par heure des publications de la chaîne Telegram @pravdafrcom23 :

Nombre de publications par heure de la chaîne Telegram pravda-fr[.]com

> **[Figure F6 - p. 7] Graphique (barres) - publications par heure de la chaîne Telegram @pravdafrcom**
>
> Série temporelle en barres très denses : nombre de publications par heure (axe vertical « size », graduations 0 à 9) de la chaîne Telegram @pravdafrcom, du 28 juin 2023 au 19 septembre 2023 (axe horizontal « date », repères Jul 16 2023, Jul 30, Aug 13, Aug 27, Sep 10). Légende : pravdafrcom (vert).
> Le graphique est quasiment plein : la valeur par heure oscille entre 1 et 9 et atteint le plafond de 9 pendant de longues plages continues à partir de la mi-juillet .
>
> Ce que la figure enseigne : la chaîne publie de façon quasi ininterrompue avec un plafond de neuf contenus par heure, signature d'un envoi automatisé avec limite de débit (le rapport note une volumétrie maximale de neuf contenus par heure).

La chaîne Telegram @pravdafrcom publie quasiment de manière ininterrompue, avec un maximum de neuf contenus par heure. Le graphique ci-dessous renseigne sur la plage horaire de ces publications. Sur l’ensemble de la période étudiée, la chaîne a publié au moins une fois à toutes les heures de la journée. Une baisse de volumétrie s’observe entre 1h et 6h du matin avec une moyenne de 149 contenus diffusés sur cette tranche, contre 506 sur le reste de la journée.

Du 28 juin 2023 (date de création de la chaîne Telegram @pravdadecom, la première des cinq), au 19 septembre 2023. Créée le 3 juillet, 119 abonnés au 26 décembre 2023.

<!-- page 8 -->
Plage horaire des publications sur la chaîne Telegram pravda-fr[.]com

> **[Figure F7 - p. 8] Graphique (barres) - répartition horaire des publications de @pravdafrcom**
>
> Histogramme : nombre cumulé de publications de la chaîne Telegram @pravdafrcom selon l'heure de la journée (axe horizontal « hour », 0 à 23 ; axe vertical « size », graduations 0 à 600). Légende : pravdafrcom (vert).
> Valeurs approximatives lues sur le graphique : environ 385 à 0 h, 270 à 1 h, 125 à 2 h, 100 à 3 h, 75 à 4 h, 105 à 5 h, 220 à 6 h, 405 à 7 h, 485 à 8 h, 510 à 9 h ; puis un plateau d'environ 500 à 575 de 10 h à 23 h (maximum d'environ 575 vers 11 h, environ 465 à 23 h).
> Le texte du rapport donne : au moins une publication à toutes les heures de la journée ; moyenne de 149 contenus entre 1 h et 6 h contre 506 sur le reste de la journée.
>
> Ce que la figure enseigne : l'activité est continue 24 h sur 24, avec seulement une baisse nocturne (1 h-6 h) ; un rythme sans pause comparable à celui d'un script, non à celui d'une rédaction humaine.

Sur la chaîne @pravdaencom, des résultats répondant à une logique similaire sont également observés :

Nombre des publications par heure de la chaîne Telegram pravda-en[.]com

> **[Figure F8 - p. 8] Graphique (barres) - publications par heure de la chaîne Telegram @pravdaencom**
>
> Série temporelle en barres : nombre de publications par heure (axe vertical « size », graduations 0 à 10) de la chaîne Telegram @pravdaencom, sur la même période (repères Jul 16 2023, Jul 30, Aug 13, Aug 27, Sep 10). Légende : pravdaencom (orange).
> Le graphique est presque entièrement rempli jusqu'à 9 publications par heure, avec de rares creux ; une barre fine dépasse à 10 (le texte parle de dix contenus dans l'heure, à deux reprises, « possiblement en raison d'une erreur d'automatisation »).
>
> Ce que la figure enseigne : même logique que pravda-fr, avec un plafond de neuf publications par heure presque toujours saturé ; les deux dépassements à dix sont lus comme un défaut du script d'automatisation.

Plage horaire des publications sur la chaîne Telegram pravda-en[.]com

> **[Figure F9 - p. 8] Graphique (barres) - répartition horaire des publications de @pravdaencom**
>
> Histogramme : nombre cumulé de publications de la chaîne Telegram @pravdaencom selon l'heure de la journée (axe horizontal « hour », 0 à 23 ; axe vertical « size », graduations 0 à 700). Légende « channel » avec cinq chaînes (pravdadecom, pravdaencom, pravdaescom, pravdafrcom, pravdaplcom), seule pravdaencom (orange) étant affichée.
> Valeurs approximatives : environ 650 à 0 h, puis une légère baisse à environ 550-590 entre 3 h et 5 h, puis environ 670-700 de 7 h à 23 h. Le texte donne 593 contenus en moyenne entre 1 h et 6 h contre 684 sur le reste de la journée.
>
> Ce que la figure enseigne : une légère baisse nocturne seulement, plus faible que pour pravdafrcom : le flux est presque uniforme sur 24 h, ce qui confirme une diffusion automatisée.

Les graphiques ci-dessus soulignent également que sur cette chaîne Telegram, le nombre de contenus publiés par heure n’excède jamais neuf publications, sauf deux fois24, possiblement en raison d’une erreur d’automatisation. D’autre part, une légère baisse d’activité est aussi décelée entre 1h et 6h, avec 593 contenus publiés en moyenne, contre 684 contenus sur le reste de la journée. Sur VK, chaque site possède également un compte sur lequel sont publiés massivement des vidéos et des textes provenant des mêmes sources susmentionnées. Toutefois, sur ces deux plateformes, les contenus diffusés n’engendrent qu’une volumétrie excessivement faible, voire dans certains cas, nulle.

Dix contenus publiés dans l’heure.

<!-- page 9 -->
Une automatisation peu rigoureuse Le ou les administrateur(s) du réseau « Portal Kombat » semble(nt) avoir commis plusieurs erreurs dans l’automatisation de la diffusion de ses contenus, qui permettent de déduire l’implication d’un acteur d’origine russe ou a minima russophone au sein de ce réseau. Ainsi, pour l’écosystème « pravda », les contenus issus d’agences de presse russes (comme RIA Novosti, TASS ou Izvestia) semblent avoir été simplement dupliqués puis traduits via l’utilisation d’un logiciel de traduction automatique, laissant apparaître certaines formulations typiques d’une traduction automatisée du russe vers le français25. Plus encore, sur les comptes VK associés à ces sites, certaines vidéos ont conservé leurs titres en cyrillique (cf. ci-dessous).

Exemples de vidéos non traduites sur les chaines VK de pravda-fr[.]com et pravda-de[.]com

> **[Figure F10 - p. 9] Capture de publications VK - vidéos non traduites (encadrés rouges)**
>
> Deux captures de listes de vidéos de comptes VK, avec des encadrés rouges tracés par VIGINUM autour des titres en cyrillique restés non traduits.
> - Compte « pravdafrcom » (gauche) : liste de vidéos avec vignette, durée, nombre de vues et ancienneté. Encadrés en rouge : « Владимир Путин посетил Национальный медицинский исследовательский центр детской гематологии,… » [Vladimir Poutine a visité le Centre national de recherche médicale en hématologie pédiatrique…] (0:56, 4 vues, « il y a quatre heures ») et « Президент принимает в загородной резиденции Ново-Огарёво заместителя председателя Центрального… » [Le président reçoit dans sa résidence de campagne de Novo-Ogarevo le vice-président du Central…, fin tronquée dans la capture] (1:05, 1 vue). Titres traduits en français, non encadrés : « Les Palestiniens de la ville de Khan Younès évaluent les dommages causés à leurs maisons par les frappes aérienn… » (1 vue) ; « Lavrov : « L'objectif de l'Occident est de provoquer des émeutes en Russie et d'opposer la population au… » » (1 vue).
> - Compte « pravdadecom » (droite) : titres majoritairement en allemand, dont « Im Stawropol-Territorium führen Mitarbeiter des PMC Wagner, die im Urlaub sind, im Kinderlager „Start" in ihrer… », « Die US-Amerikaner haben eine Videovisualisierung (… », « Die letzten Sekunden des Lebens zweier Ukrainer, die die Frequenz 149.200 hätten erreichen können, sich aber f… » ; encadré en rouge : « Двое мужчин с огнестрельным оружием захватили школу в Гамбурге, сообщает Bild » [Deux hommes armés ont pris d'assaut une école à Hambourg, selon Bild] (0:11, 4 vues, « il y a cinq heures »). Les vues affichées vont de 1 à 16.
>
> Ce que la figure enseigne : les vidéos reprises de sources russes gardent leur titre cyrillique malgré la traduction automatique des autres titres, une erreur d'automatisation qui, selon VIGINUM, permet de déduire l'implication d'un acteur d'origine russe ou a minima russophone. Les vues très faibles (1 à 16) confirment que ces comptes VK n'engendrent qu'une volumétrie excessivement faible.

AUDIENCES CIBLÉES ET MESSAGES

Des audiences ciblées selon la localisation Comme le réseau « Portal Kombat » ne produit aucun contenu original, sa « valeur ajoutée » réside dans la sélection des contenus pour chaque site. Pour les portails de l’écosystème « historique », les contenus diffusés semblent relativement peu politisés, voire inoffensifs. Ils relatent principalement, selon la localisation ciblée, des informations liées à des évènements sportifs, festifs, ou encore institutionnels. Certaines informations sont communes car issues de médias d’État ou de bloggeurs populaires, mais les contenus diffusés pour le site belgorod- news[.]net, sont différents de ceux publiés, par exemple, sur le portail qui cible la péninsule du Kamtchatka, kamchatka-news[.]net. Néanmoins, en raison des sources utilisées, les sites qui ciblent la Russie propagent de facto des contenus politiques qui vantent, entre autres, le bien-fondé de « l’opération militaire spéciale » ou qui entretiennent le clivage Russie/Occident. VIGINUM a également détecté de nombreux contenus faisant la promotion régulière des services de sécurité russes, notamment du FSB26. Ce type de contenus est diffusé dans le flux abondant des centaines d’autres articles traitant d’évènements non-politiques.

Par exemple : https://archive.ph/S46CO. Dans cet article, copié-collé du site tass[.]ru, concernant un déplacement au Proche- Orient de l’ancienne ministre de l’Europe et des Affaires étrangères, Catherine COLONNA, est désignée tantôt par « Le ministre » ou « La Colonne ». De plus, son ministère est qualifié de « ministère des affaires étrangères de la République ». Des centaines, voire des milliers d’articles concernant ce sujet peuvent être recensés sur certains sites.

<!-- page 10 -->
Après l’invasion de l’Ukraine, une volonté d’accroître la pression informationnelle Pour l’écosystème « -news.ru », la stratégie de diffusion semble être davantage offensive. En effet, les sites créés depuis l’invasion de l’Ukraine ne sont plus de simples portails relatant des informations générales ou d’actualité locale, mais se révèlent être de véritables « caisses de résonance » du dispositif d’influence numérique russe et ce, suivant un maillage informationnel méticuleux du territoire ukrainien. En effet, certaines des localités ciblées semblent l’avoir été du fait de leur caractéristiques démographiques (territoires habités par des minorités russophones), notamment dans les régions proches de la ligne de front ou en territoires occupés. Il peut également être ici mentionné la présence du site tiraspol-news[.]ru qui cible, entre autres, des villes à la frontière non reconnue entre la Moldavie et la république sécessionniste de Transnistrie comme Dubăsari, qui ne recense qu’environ 28 000 habitants. Si certaines informations peuvent paraître anodines, la majeure partie des contenus diffusés vise à principalement amplifier le ressentiment des populations locales russes à l’encontre des autorités ukrainiennes et informer sur les opérations militaires en cours, comme en témoigne la rubrique « correspondants militaires ».

VIGINUM a cartographié précisément les 41 localités qui possèdent un portail d’information « -news.ru » (cf. ci-dessus). Ces sites n’ont pas été enregistrés à la même date : ceux enregistrés en avril 2022 ciblent des localités plus à l’est, tandis que ceux créés en décembre 2022 viennent davantage compléter ce « quadrillage informationnel » en ciblant d’autres villes à l’ouest comme par exemple Lviv ou Ternopil. Par ailleurs, certaines villes stratégiques possèdent au moins deux portails comme Donetsk ou Louhansk.

> **[Figure F11 - p. 10] Carte - villes ukrainiennes ciblées par un portail « -news.ru »**
>
> Carte de l'Ukraine intitulée « VILLES CIBLÉES PAR UN PORTAIL D'INFORMATION « -NEWS.RU » », avec les pays voisins (Biélorussie, Russie, Pologne, Roumanie, Moldavie) et l'échelle 100 km. Légende : pastille bleue = « Portail créé en avril 2022 » ; pastille rose/rouge = « Portail créé en décembre 2022 ».
> Villes nommées sur la carte : Kiev, Kharkiv, Louhansk, Donetsk, Dnipro, Zaporijia, Marioupol, Berdiansk, Kherson, Odessa, Tiraspol (frontière avec la Transnistrie), à l'est et au sud ; et, à l'ouest, Lutsk, Lviv, Ternopil, Oujhorod (pastilles roses).
> Répartition lisible : les pastilles bleues (avril 2022) couvrent le centre, l'est, le sud et le nord (Kiev, Kharkiv, Donbass, côte de la mer d'Azov et de la mer Noire, Tiraspol) ; environ 8 pastilles roses (décembre 2022) sont concentrées dans l'ouest du pays (Lutsk et sa voisine, Lviv, Ternopil et ses voisines, Oujhorod). Plusieurs villes de l'est (Louhansk, Donetsk) portent deux pastilles proches, c'est-à-dire deux portails.
>
> Ce que la figure enseigne : 41 localités sont couvertes par un maillage informationnel du territoire ukrainien, d'abord à l'est et au sud (avril 2022, régions proches de la ligne de front et territoires occupés), puis complété à l'ouest en décembre 2022 ; la présence de tiraspol-news[.]ru montre un ciblage de la frontière moldave.

<!-- page 11 -->
Une extension vers les audiences internationales Pour l’écosystème « pravda », la logique de dissémination des narratifs pro-Kremlin est similaire. Les sources étant systématiquement affichées en pied d’article, VIGINUM a ainsi pu déterminer avec précision la nature des contenus diffusés par le site pravda-fr[.]com. La liste ci-contre présente les 20 chaînes Telegram francophones qui constituent, sur la période de temps analysée, ses principales sources. De cette liste, sont volontairement retirés les contenus directement issus des sites de médias d’État russes, bien que ces derniers constituent une large part des sources du site. Ainsi, via ces différents canaux, les articles diffusés servent principalement à la couverture du conflit en Ukraine en présentant d’une part positivement l’invasion russe aux populations occidentales et, en dénigrant d’autre part, l’Ukraine et ses dirigeants, régulièrement présentés comme « corrompus », « nazis » ou « incompétents ». Ces chaînes, publient également sur d’autres thématiques proches Liste des 20 chaînes Telegram des sphères complotistes francophones, qui tendent à remettre en francophones les plus utilisées comme cause la parole politique, les médias ou encore les différentes sources d'articles de pravda-fr[.]com décisions prises au sein des organisations internationales comme l’OTAN, l’ONU ou l’UE par exemple. Enfin, une part importante des contenus diffusés concerne la reprise opportuniste de thématiques d’actualité pouvant contribuer à diverses polémiques. Ainsi, pendant l’été 2023, le site pravda-fr[.]com a publié de nombreux contenus traitant des différentes crises survenues dans des pays comme le Niger ou le Gabon, dénigrant la présence française au Sahel ou promouvant un accroissement de la coopération entre la Russie et le continent Africain. Ce type de contenu peut ensuite être exploité sur d’autres canaux « d’information » ce qui pourrait participer, de fait, à cliver le débat public francophone. Par exemple, VIGINUM a récemment détecté l’insertion du site pravda-fr[.]com comme source d’un article Wikipédia traitant de la situation géopolitique en mer Rouge et pouvant être sujet à polémique 27. La sélection précise de ces sources pro-russes, certaines ne possédant que très peu d’abonnés et parfois créées quelques mois seulement avant la mise en activité des sites 28, atteste d’un véritable travail de ciblage pour diffuser les narratifs souhaités.

> **[Figure F12 - p. 11] Tableau - 20 chaînes Telegram francophones sources de pravda-fr[.]com**
>
> Tableau « Source / Articles » : les 20 chaînes Telegram francophones les plus utilisées comme sources d'articles de pravda-fr[.]com sur la période analysée (hors contenus directement issus des médias d'État russes, retirés volontairement).
>
> | Source (chaîne Telegram) | Articles |
> |---|---|
> | BrainlessChanel | 4118 |
> | Vbachir | 4024 |
> | russiejournal | 2809 |
> | russosphere | 1790 |
> | tass_fr | 1375 |
> | RTenfrancais | 986 |
> | ErwanKastel | 674 |
> | StreetReporters1 | 664 |
> | infodefFRANCE | 544 |
> | geopolitiqueprofonde | 523 |
> | boriskarpovblog | 451 |
> | ukr_leaks_fr | 428 |
> | russieukrainemondeFR | 279 |
> | russieenlive | 263 |
> | crises_ponctuelles | 249 |
> | actualiteFR | 234 |
> | beaurfabrice | 215 |
> | chroniques_conflit_ukraine | 139 |
> | EgountchiBehanzinOfficiel | 139 |
> | kompromatmedia | 121 |
> | dedollarisationdumonde | 119 |
>
> (Le tableau affiché compte vingt et une lignes pour un intitulé annoncé de 20 chaînes ; les lignes sont reproduites telles que lues.)
>
> Ce que la figure enseigne : l'alimentation de pravda-fr[.]com est concentrée sur quelques chaînes Telegram prorusses francophones (BrainlessChanel et Vbachir fournissent à elles seules 8 142 articles), plus des relais de médias d'État (tass_fr, RTenfrancais) ; le choix de sources parfois peu suivies ou récentes (par exemple @dedollarisationdumonde, créée le 20 avril 2023, 474 abonnés) atteste, selon VIGINUM, d'un ciblage délibéré des narratifs.

*** Dispositif informationnel structuré et coordonné, le réseau « Portal Kombat » inscrit résolument son activité dans le contexte du conflit entre la Russie et l’Ukraine par la diffusion de contenus de propagande pro-russe à grande échelle. Si ce réseau ciblait historiquement des populations ukrainiennes et russes, son action s’est désormais tournée vers les audiences internationales, en particulier à l’encontre des pays occidentaux ayant affiché leur soutien à Kiev.

Compte tenu de ses caractéristiques techniques, des procédés mis en œuvre ainsi que des objectifs poursuivis, ce réseau constitue une ingérence numérique étrangère.

L’article Wikipédia intitulé « Opération Gardien de la prospérité » créé le 22 décembre 2023, a été modifié le lendemain par l’utilisateur @Lataupefr, qui a inséré deux articles de pravda-fr[.]com ayant comme sources les chaines Telegram pro-russes @BrainlessChanel et @kompromatmedia. Cf. modifications: https://fr.wikipedia.org/w/index.php?title=Opération_Gardien_de_la_prospérité&diff=prev&oldid=210810683 La chaîne @dedollarisationdumonde, par exemple, créée le 20 avril 2023, ne possède que 474 abonnés.

<!-- page 12 -->
ANNEXE I : LISTE DES SITES IDENTIFIÉS DU RÉSEAU « PORTAL KOMBAT »29

> **[Figure F13 - p. 12] Tableau - code couleur de l'annexe I (légende)**
>
> L'annexe I liste les sites du réseau (nom de domaine, date de création, actif oui/non) sur les pages 12 à 15 ; le texte du tableau est repris ci-dessous dans l'ordre du PDF. La couleur de la cellule « Nom de domaine » code l'écosystème, selon la légende affichée page 12 :
> - rouge : écosystème « historique » Russie ;
> - jaune : écosystème « historique » Ukraine ;
> - bleu : écosystème « -news.ru » ;
> - gris clair : écosystème « pravda ».
> Dans la colonne « Actif », les cellules « non » sont sur fond gris foncé ; ce sont presque toutes des sites de l'écosystème historique Ukraine créés en 2013 (la seule exception en rouge est moskva-news[.]com, 2013-03-09).
>
> Ce que la figure enseigne : la colonne de couleur permet de lire d'un coup d'oeil l'appartenance de chaque domaine. À noter : certains domaines en .ru sont rangés avec l'écosystème « historique » Russie (voir la figure suivante).

Nom de domaine Date de création Actif piter-news[.]net 2013-03-07 12:13:21 oui lenta.kharkiv[.]ua 2013-03-07 13:14:01 oui Légende moskva-news[.]com 2013-03-09 00:00:00 non Ecosystème "historique" Russie uanews.kharkiv[.]ua 2013-03-18 16:34:00 oui Ecosystème "historique" topnews.kiev[.]ua 2013-03-24 06:56:46 oui Ukraine topnews.odessa[.]ua 2013-03-30 12:09:59 oui Ecosystème "-news.ru" uanews.odessa[.]ua 2013-03-30 21:33:01 oui Ecosystème "pravda" dneprnews.com[.]ua 2013-04-01 13:52:01 oui uanews.dp[.]ua 2013-04-01 13:52:09 oui topnews.zp[.]ua 2013-04-01 13:54:00 oui uanews.zp[.]ua 2013-04-01 13:54:03 oui lenta.te[.]ua 2013-04-08 22:19:45 oui lenta.lviv[.]ua 2013-04-11 00:00:00 oui lenta.donetsk[.]ua 2013-04-19 00:00:00 non topnews.uz[.]ua 2013-05-01 00:00:00 non topnews.sebastopol[.]ua 2013-05-11 00:00:00 non topnews.sumy[.]ua 2013-05-11 00:00:00 non uanews.crimea[.]ua 2013-05-11 00:00:00 non uanews.sumy[.]ua 2013-05-11 00:00:00 non lenta.if[.]ua 2013-05-11 00:00:00 non topnews.km[.]ua 2013-05-11 00:00:00 non topnews.ks[.]ua 2013-05-11 00:00:00 non topnews.lg[.]ua 2013-05-11 00:00:00 non uanews.ck[.]ua 2013-05-11 00:00:00 non uanews.cn[.]ua 2013-05-11 00:00:00 non uanews.cv[.]ua 2013-05-11 00:00:00 non uanews.if[.]ua 2013-05-11 00:00:00 non uanews.km[.]ua 2013-05-11 00:00:00 non uanews.kr[.]ua 2013-05-11 00:00:00 non uanews.ks[.]ua 2013-05-11 00:00:00 non uanews.lg[.]ua 2013-05-11 00:00:00 non uanews.pl[.]ua 2013-05-11 00:00:00 non uanews.rv[.]ua 2013-05-11 00:00:00 non uanews.uz[.]ua 2013-05-11 00:00:00 non uanews.vn[.]ua 2013-05-11 00:00:00 non uanews.zt[.]ua 2013-05-11 00:00:00 non uanews.donetsk[.]ua 2013-05-11 00:00:00 oui uanews.lviv[.]ua 2013-05-11 00:00:00 oui topnews.volyn[.]ua 2013-05-11 00:00:00 oui topnews.cv[.]ua 2013-05-11 00:00:00 oui uanews.te[.]ua 2013-05-11 00:00:00 oui

VIGINUM n’a pas été en mesure de retrouver l’historique Whois de tous les sites, en particulier ceux ciblant l’Ukraine et appartenant à l’écosystème « historique », qui sont pour la plupart inactifs ou n’existent plus. À partir d’archives, de comparaison avec d’autres sites similaires ainsi qu’en se référant à la création des comptes de réseaux sociaux affiliés (notamment VK), VIGINUM estime que ces portails ont probablement été créés entre les mois d’avril et mai 2013.

<!-- page 13 -->
uanews.volyn[.]ua 2013-05-11 00:00:00 non kiev-news.com[.]ua 2013-05-11 00:00:00 non niknews.com[.]ua 2013-05-11 00:00:00 non topnews.zt[.]ua 2013-05-11 11:54:23 oui nikolaevnews.com[.]ua 2013-05-11 12:02:24 oui topnews.pl[.]ua 2013-05-11 12:03:24 oui topnews.rv[.]ua 2013-05-11 12:03:29 oui topnews.cn[.]ua 2013-05-11 12:16:24 oui topnews.ck[.]ua 2013-05-12 23:00:00 oui topnews.kr[.]ua 2013-05-12 23:00:00 oui topnews.vn[.]ua 2013-05-14 23:00:00 oui crimea-news[.]com 2013-11-05 05:17:18 oui barnaul-news[.]net 2013-12-02 16:01:10 oui chelyabinsk-news[.]net 2013-12-02 16:01:20 oui irkutsk-news[.]net 2013-12-02 16:01:31 oui izhevsk-news[.]net 2013-12-02 16:01:41 oui kazan-news[.]net 2013-12-02 16:01:52 oui khabarovsk-news[.]net 2013-12-02 16:02:02 oui krasnodar-news[.]net 2013-12-02 16:02:12 oui krasnoyarsk-news[.]net 2013-12-02 16:02:23 oui nn-news[.]net 2013-12-02 16:02:33 oui novosibirsk-news[.]net 2013-12-02 16:02:43 oui omsk-news[.]net 2013-12-02 16:02:54 oui perm-news[.]net 2013-12-02 16:03:04 oui rostov-news[.]net 2013-12-02 16:03:15 oui samara-news[.]net 2013-12-02 16:03:26 oui saratov-news[.]net 2013-12-02 16:03:36 oui sochi-news[.]net 2013-12-02 16:03:47 oui tolyatti-news[.]net 2013-12-02 16:03:57 oui tyumen-news[.]net 2013-12-02 16:04:07 oui ufa-news[.]net 2013-12-02 16:04:18 oui ulyanovsk-news[.]net 2013-12-02 16:04:28 oui ural-news[.]net 2013-12-02 16:04:39 oui vladivostok-news[.]net 2013-12-02 16:04:49 oui volgograd-news[.]net 2013-12-02 16:04:59 oui voronezh-news[.]net 2013-12-02 16:05:10 oui yaroslavl-news[.]net 2013-12-02 16:05:20 oui sevastopol-news[.]com 2015-06-04 11:22:09 oui msk-news[.]net 2018-04-09 19:11:17 oui astrakhan-news[.]net 2018-04-09 19:11:29 oui arkhangelsk-news[.]net 2018-04-09 19:12:16 oui belgorod-news[.]net 2018-04-09 19:12:27 oui vladimir-news[.]net 2018-04-09 19:12:38 oui vologda-news[.]net 2018-04-09 19:12:49 oui dagestan-news[.]net 2018-04-09 19:12:59 oui ivanovo-news[.]net 2018-04-09 19:13:10 oui kaliningrad-news[.]net 2018-04-09 19:13:21 oui kirov-news[.]net 2018-04-09 19:13:32 oui murmansk-news[.]net 2018-11-29 19:57:06 oui kemerovo-news[.]net 2018-11-29 19:57:17 oui penza-news[.]net 2018-11-29 19:57:28 oui

<!-- page 14 -->
orenburg-news[.]net 2018-11-29 19:57:39 oui orel-news[.]net 2018-11-29 19:57:54 oui stavropol-news[.]net 2018-11-29 19:58:05 oui smolensk-news[.]net 2018-11-29 19:58:17 oui tomsk-news[.]net 2018-11-29 19:58:27 oui tver-news[.]net 2018-11-29 19:58:39 oui ryazan-news[.]net 2018-11-29 19:58:52 oui tula-news[.]net 2018-11-29 19:59:03 oui chita-news[.]net 2018-11-29 20:23:05 oui kursk-news[.]net 2018-11-29 20:23:16 oui lipetsk-news[.]net 2018-11-29 20:23:27 oui saransk-news[.]net 2018-11-29 20:23:39 oui kostroma-news[.]net 2018-11-29 20:23:50 oui yamal-news[.]net 2018-11-29 20:24:01 oui tambov-news[.]net 2018-11-29 20:24:12 oui kaluga-news[.]net 2018-11-29 20:24:23 oui sakhalin-news[.]net 2018-11-29 20:24:34 oui cheb-news[.]net 2018-11-29 20:24:45 oui ugra-news[.]net 2018-11-29 20:24:56 oui yakutsk-news[.]net 2018-11-29 20:25:09 oui kamchatka-news[.]net 2018-11-29 20:25:21 oui karelia-news[.]net 2018-11-29 20:25:32 oui komi-news[.]net 2018-11-29 20:25:44 oui udmurt-news[.]net 2018-11-29 20:25:55 oui kalmykia-news[.]net 2018-11-29 20:26:07 oui tuva-news[.]net 2018-11-29 20:26:18 oui baikal-news[.]net 2018-11-29 20:26:31 oui pskov-news[.]net 2018-11-30 16:30:15 oui altay-news[.]net 2018-12-26 21:01:19 oui ingushetiya-news[.]net 2018-12-26 21:01:32 oui adygheya-news[.]net 2018-12-26 21:01:36 oui nalchik-news[.]net 2018-12-26 21:01:44 oui mariel-news[.]net 2018-12-26 21:01:55 oui cherkessk-news[.]net 2018-12-26 21:02:06 oui vladikavkaz-news[.]net 2018-12-26 21:02:17 oui abakan-news[.]net 2018-12-26 21:02:27 oui grozny-news[.]net 2018-12-26 21:02:38 oui amur-news[.]net 2018-12-26 21:02:49 oui bryansk-news[.]net 2018-12-26 21:03:00 oui kurgan-news[.]net 2018-12-26 21:03:11 oui birobidzhan-news[.]net 2018-12-26 21:03:22 oui nao-news[.]net 2018-12-26 21:03:33 oui chukotka-news[.]net 2018-12-26 21:03:44 oui novgorod-news[.]net 2018-12-26 21:03:54 oui magadan-news[.]net 2019-01-10 14:40:58 oui novyny.kr[.]ua 2019-01-19 19:18:52 oui novyny.zt[.]ua 2019-01-19 19:19:02 oui gazeta.kharkiv[.]ua 2019-01-19 19:19:08 oui cherkassy-news[.]ru 2022-04-03 14:18:02 oui kherson-news[.]ru 2022-04-03 14:18:02 oui lnr-news[.]ru 2022-04-03 14:18:02 oui

<!-- page 15 -->
news-kharkov[.]ru 2022-04-03 14:18:02 oui poltava-news[.]ru 2022-04-03 14:18:02 oui vin-news[.]ru 2022-04-03 14:18:02 oui zp-news[.]ru 2022-04-03 14:18:02 oui chernigov-news[.]ru 2022-04-03 14:18:03 oui dnepr-news[.]ru 2022-04-03 14:18:03 oui dnr-news[.]ru 2022-04-03 14:18:03 oui kirovograd-news[.]ru 2022-04-03 14:18:03 oui news-kiev[.]ru 2022-04-03 14:18:03 oui news-odessa[.]ru 2022-04-03 14:18:03 oui nikolaev-news[.]ru 2022-04-03 14:18:03 oui sumy-news[.]ru 2022-04-03 14:18:03 oui zhitomir-news[.]ru 2022-04-03 14:18:03 oui berdyansk-news[.]ru 2022-04-18 11:44:51 oui donetsk-news[.]ru 2022-04-18 11:44:51 oui lugansk-news[.]ru 2022-04-18 11:44:51 oui mariupol-news[.]ru 2022-04-18 11:44:51 oui melitopol-news[.]ru 2022-04-18 11:44:51 oui alchevsk-news[.]ru 2022-04-18 17:45:51 oui bc-news[.]ru 2022-04-18 17:45:51 oui dneprodzerzhinsk- news[.]ru 2022-04-18 17:45:51 oui gorlovka-news[.]ru 2022-04-18 17:45:51 oui kramatorsk-news[.]ru 2022-04-18 17:45:51 oui kremenchug-news[.]ru 2022-04-18 17:45:51 oui krivoy-rog-news[.]ru 2022-04-18 17:45:51 oui news-makeevka[.]ru 2022-04-18 17:45:51 oui nikopol-news[.]ru 2022-04-18 17:45:51 oui pavlograd-news[.]ru 2022-04-18 17:45:51 oui slavyansk-news[.]ru 2022-04-18 17:45:51 oui tiraspol-news[.]ru 2022-04-26 08:55:50 oui norilsk-news[.]ru 2022-11-18 17:01:26 oui nabchelny-news[.]ru 2022-11-22 16:20:55 oui nk-news[.]ru 2022-11-22 16:20:55 oui tagil-news[.]ru 2022-11-22 16:20:55 oui news-surgut[.]ru 2022-11-22 16:20:56 oui chernovcy-news[.]ru 2022-12-17 17:08:17 oui if-news[.]ru 2022-12-17 17:08:17 oui rovno-news[.]ru 2022-12-17 17:08:17 oui volyn-news[.]ru 2022-12-17 17:08:17 oui khmelnitskiy-news[.]ru 2022-12-17 17:08:18 oui lvov-news[.]ru 2022-12-17 17:08:18 oui ternopol-news[.]ru 2022-12-17 17:08:18 oui uzhgorod-news[.]ru 2022-12-17 17:08:18 oui pravda-de[.]com 2023-02-22 17:31:51 oui pravda-en[.]com 2023-06-24 10:22:27 oui pravda-es[.]com 2023-06-24 10:22:27 oui pravda-fr[.]com 2023-06-24 10:22:27 oui pravda-pl[.]com 2023-06-24 10:22:27 oui news-balashiha[.]ru 2023-07-11 16:04:48 oui volzhskiy-news[.]ru 2023-07-11 16:04:48 oui

> **[Figure F14 - p. 15] Tableau - code couleur de l'annexe I (page 15)**
>
> Sur la page 15 de l'annexe I, les couleurs des cellules « Nom de domaine » se répartissent ainsi.
> - Bleu (écosystème « -news.ru ») : news-kharkov[.]ru jusqu'à tiraspol-news[.]ru (créés les 3, 18 et 26 avril 2022), puis chernovcy-news[.]ru, if-news[.]ru, rovno-news[.]ru, volyn-news[.]ru, khmelnitskiy-news[.]ru, lvov-news[.]ru, ternopol-news[.]ru, uzhgorod-news[.]ru (créés le 17 décembre 2022).
> - Rouge (écosystème « historique » Russie), bien que le domaine soit en .ru : norilsk-news[.]ru (2022-11-18), nabchelny-news[.]ru, nk-news[.]ru, tagil-news[.]ru, news-surgut[.]ru (2022-11-22), news-balashiha[.]ru et volzhskiy-news[.]ru (2023-07-11), tous des villes russes.
> - Gris clair (écosystème « pravda ») : pravda-de[.]com (2023-02-22), pravda-en[.]com, pravda-es[.]com, pravda-fr[.]com, pravda-pl[.]com (tous 2023-06-24).
> En bas de la page 14, les trois domaines en .ru (cherkassy-news[.]ru, kherson-news[.]ru, lnr-news[.]ru, créés le 2022-04-03) sont bleus, et novyny.kr[.]ua, novyny.zt[.]ua, gazeta.kharkiv[.]ua (2019-01-19) sont jaunes (historique Ukraine). Sur les pages 13 à 15, seules trois lignes en haut de la page 13 sont inactives (« non ») : uanews.volyn[.]ua, kiev-news.com[.]ua, niknews.com[.]ua ; tous les autres domaines sont « oui ».
>
> Ce que la figure enseigne : l'écosystème « historique » Russie continue de s'enrichir après 2022 (cinq domaines en novembre 2022, deux en juillet 2023) avec des domaines en .ru, ce qui montre que le suffixe du domaine ne suffit pas à classer un site : le classement repose sur la cible (localité russe) et le gabarit.

<!-- page 16 -->
ANNEXE II : SCHÉMA DU RÉSEAU « PORTAL KOMBAT »

> **[Figure F15 - p. 16] Schéma de l'infrastructure du réseau « Portal Kombat » (© VIGINUM 2024)**
>
> Schéma titré « Schéma du réseau « Portal Kombat » », sur toute la page, en trois zones.
>
> Zone du haut, encadrée en rouge et étiquetée « AS49352 » avec le drapeau de la Russie (système autonome d'hébergement) :
> - Un certificat « SSL » portant l'ETag « 640ba6a8-d9c » est relié par des flèches à 8 serveurs : 178.21.13.3, 178.21.13.32, 178.21.13.33, 178.21.13.34, 178.21.13.35, 178.21.14.92, 178.21.14.93, 178.21.15.204.
> - Une flèche mène aussi vers le serveur 176.99.6.152, relié à un site « regrucolo.ru ».
> - Un groupe en pointillés de trois serveurs : 178.21.15.41, 178.21.15.42, 178.21.15.183 (hébergement des 41 sites « -news.ru » d'après la note 9 du rapport, ces trois IP étant citées).
>
> Zone du bas, trois écosystèmes (étiquettes bleu clair) :
> 1. « Ecosystème depuis 2013 » (drapeaux Ukraine et Russie), relié en pointillé à l'ETag du certificat SSL. Il utilise : youtube.com/@user-lb1vs2sf5x (chaîne YouTube diffusant des « Vidéos FSB ») ; un compte X/Twitter « {nom ville ukr}news777 / {nom ville ukr}news888 » ; un compte VK « {nom ville ukr}news777 / {nom ville ukr}news888 ». Ses sites, regroupés dans un cadre violet en pointillés, sont : « {ville}-news.net / {ville}-news.com » (x86), « topnews.{code ville ukr}.ua » (x15), « uanews.{code ville ukr}.ua » (x20), « lenta.{code ville ukr}.ua » (x5), « {code ville ukr}news.com.ua » (x3), « novyny.{code ville ukr}.ua » (x2) et « gazeta.kharkiv.ua » (sans multiplicateur lisible) ; cette association des multiplicateurs aux modèles de domaine est une lecture de la disposition (les libellés sont placés sous les icônes) et peut être décalée d'un cran. Ces sites « utilisent » des identifiants Google Analytics : UA-39144183, UA-42267760, UA-47448818, UA-6961125, UA-73883606 (mis en gras), UA-85626226, UA-100903237.
> 2. « Ecosystème depuis 2022 » (drapeau Ukraine), relié par une flèche aux trois serveurs 178.21.15.41/.42/.183. Ses sites : « {ville ukr}-news.ru » (x37) et « news-{ville ukr}.ru » (x4), qui « utilisent » les identifiants Google Analytics UA-19131147, UA-3380909, UA-48689684, UA-55552418, UA-56961125 ; ils « utilisent une favicon » (trait violet) et la chaîne « <div class="logo_big">ZOV</div> » (ou les commentaires <!--/LiveInternet--><!-- Yandex.Metrika counter --> et <!-- /Yandex.Metrika counter --><!-- Top.Mail.Ru counter -->) « est dans code source ». Un compteur Top.Mail.Ru « top-fwz1.mail.ru/counter?id={id_incremental} » est relevé avec les plages d'identifiants incrémentaux [3250778-3250810], [3276998-3277004] et 3252669.
> 3. « Ecosystème depuis 2023 » : alimenté « en contenus ciblés (en fonction du pays) » par des « Médias russes ou pro-russes (RT, RIA NOVOSTI, TASS, VESTI) » et des « Chaînes Telegram pro-russes » ; il partage la « Favicon MurmurHash3 = -200225920 » (flèche noire « est utilisée » vers l'écosystème 2023 ; un trait violet « utilisent une favicon » relie aussi les sites « -news.ru » de l'écosystème 2022 à cette favicon). Une note violette indique « Travail d'optimisation SEO / Référencement de qualité ». Il possède un compte Telegram et un compte VK « pravda{code iso pays}com ». Ses cinq sites pravda-fr.com (drapeau France), pravda-en.com (Royaume-Uni et États-Unis), pravda-de.com (Allemagne, Autriche, Suisse), pravda-es.com (Espagne) et pravda-pl.com (Pologne) sont hébergés sur une seule IP : 178.21.15.85 (AS49352).
>
> Ce que la figure enseigne : le schéma synthétise les pivots techniques qui relient les trois écosystèmes : un même système autonome russe (AS49352), des serveurs voisins (plage 178.21.13/14/15), un E-Tag (640ba6a8-d9c), une favicon identique (MurmurHash3 -200225920), le marqueur « ZOV », des identifiants Google Analytics et Top.Mail.Ru partagés, et des comptes de réseaux sociaux nommés selon une règle fixe. Ce sont des indicateurs réutilisables pour étendre la détection du réseau.

<!-- page 17 -->
À PROPOS DE VIGINUM

Créé le 13 juillet 2021 et rattaché au SGDSN, le service de vigilance et de protection contre les ingérences numériques étrangères (VIGINUM) a pour raison d’être la protection du débat public numérique touchant aux intérêts fondamentaux de la Nation.

Ce service technique et opérationnel de l’État a pour mission de détecter et caractériser les campagnes de manipulation de l’information sur les plateformes numériques, impliquant des acteurs étrangers dans le but de nuire à la France et à ses intérêts.

[Décret n° 2021-922 du 13 juillet 2021]

Service de vigilance et protection contre les ingérences numériques étrangères | SGDSN

Crédit photo couverture : photo de Bradley Jasper sur Unsplash
