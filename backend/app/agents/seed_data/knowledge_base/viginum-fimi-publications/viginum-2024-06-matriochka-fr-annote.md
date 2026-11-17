# Matriochka : une campagne prorusse ciblant les médias et la communauté des fact-checkers - rapport technique (VIGINUM, juin 2024)

Source : site internet du secrétariat général de la défense et de la sécurité nationale (SGDSN), https://www.sgdsn.gouv.fr/viginum/publications/matriochka-une-campagne-prorusse-ciblant-les-medias-et-la-communaute-des-fact  
Document original : https://www.sgdsn.gouv.fr/files/files/viginum/Publications/20240610_NP_SGDSN_VIGINUM_Matriochka_VD.pdf

> Document enrichi pour la base de connaissances : le texte du PDF est complété, à l'emplacement de chaque figure, par une annotation qui décrit son contenu (entités, relations orientées, valeurs) et la règle qu'elle illustre. Les blocs « Figure » sont des descriptions rédigées à partir des images, pas du texte original.

> Note : Les figures reproduisent des contenus de propagande et des publications de comptes inauthentiques : elles sont décrites comme des contenus diffusés, pas comme des faits. Les renvois du texte à « section 1.2 » et « section 1.3 » correspondent en réalité à la section 2.2 (contenus) et à l'annexe 3.3 (exemples de diffusion). La campagne est aussi connue sous le nom d'Overload (Check First / Reset) ; le nom Matriochka vient du collectif antibot4navalny.

<!-- page 1 -->
MATRIOCHKA Une campagne prorusse ciblant les médias et la communauté des fact-checkers

Rapport technique Juin 2024

<!-- page 2 -->
SOMMAIRE

1. Synthèse .................................................................................................................................................................... 3

2. Analyse ...................................................................................................................................................................... 4 2.1 Diffusion artificielle ou automatisée, massive & délibérée ....................................................................... 4 2.1.1 Schéma de diffusion des contenus ......................................................................................................... 5 2.1.2 Cibles des campagnes ............................................................................................................................... 6 2.2 Contenus manifestement inexacts ou trompeurs...................................................................................... 8 2.2.1 Des contenus trompeurs et mensongers cherchant à discréditer l’Ukraine et ses alliés ............ 8 2.2.2 La France, une cible privilégiée de la campagne Matriochka ............................................................ 9 2.3 Implication, directe ou indirecte, d’un acteur étranger ..........................................................................11 2.4 Atteinte aux intérêts fondamentaux de la Nation ................................................................................... 13

3. Annexes ................................................................................................................................................................... 14 3.1 Tactiques, techniques & procédures ........................................................................................................... 14 3.2 Entités & médias français dont l’identité a été usurpée ......................................................................... 15 3.3 Exemples de diffusion des contenus de la campagne ............................................................................. 15

<!-- page 3 -->
1. SYNTHÈSE

Depuis la fin de l'année 2023, VIGINUM observe et documente une campagne malveillante susceptible d’affecter le débat public numérique francophone.

Ce mode opératoire, connu en sources ouvertes sous le nom de « Matriochka » 1 (terme russe 0F

pour désigner des poupées gigognes), est actif depuis au moins septembre 2023 2. Il s’appuie 1F

sur la publication de faux contenus (reportages, graffitis, mèmes, etc.), qui font ensuite l’objet d’une diffusion coordonnée dans l’espace réponse des publications de comptes X de médias, de personnalités et de cellules de fact-checking de plus d’une soixantaine de pays. Les opérateurs de Matriochka y interpellent directement les cibles – sur X et par message électronique – pour leur demander d’enquêter sur ces faux contenus.

Les faux contenus diffusés usurpent généralement l’identité de personnalités et de médias nord-américains ou européens, dont français. S’ils propagent et amplifient majoritairement des narratifs anti-ukrainiens, ces contenus prennent également pour cible la politique française de soutien à l’Ukraine, certaines personnalités politiques françaises ainsi que les Jeux olympiques et paralympiques de Paris 2024 (JOP24).

Les investigations de VIGINUM ont permis d’établir que la majorité des contenus diffusés ont été préalablement publiés par des chaînes Telegram russes, déjà identifiées dans des manœuvres informationnelles. VIGINUM estime que l’objectif de cette campagne est probablement de décrédibiliser les médias, personnalités et cellules de fact-checking ciblés tout en promouvant des contenus servant les intérêts russes.

Au regard de ces éléments, VIGINUM considère que la campagne Matriochka, toujours en cours, réunit les critères d’une ingérence numérique étrangère.

Ce nom est issu du collectif russe « antibot4navalny », actif sur X, et qui traque les bots ukrainiens et russes depuis 2018. Leurs publications et les données associées ont été analysées pour une partie de cette investigation. Cf. https://twitter.com/antibot4navalny. La campagne a notamment été documentée par l’Agence France Presse (cf. https://factuel.afp.com/doc.afp.com.34H32VP), par Check First et Reset sous le nom d’Overload (https://checkfirst.network/wp- content/uploads/2024/06/Operation_Overload_WEB.pdf). Selon Check First, la campagne aurait a minima débuté au mois d’août 2023.

<!-- page 4 -->
2. ANALYSE

2.1 Diffusion artificielle ou automatisée, massive & délibérée Active sur la plateforme X depuis le mois de septembre 2023 a minima, la campagne « Matriochka » se caractérise par un mode opératoire en deux étapes :

- un premier groupe de comptes, appelés « seeders », publie un faux contenu sur la plateforme (cf. section 1.2) ;

- un second groupe de comptes, appelés « quoters », partage ensuite le post d’un seeder en réponse à des posts de médias, de personnalités et de cellules de fact-checking. Les quoters interpellent les individus ou organisations ciblés pour leur demander de vérifier l’authenticité ou la véracité des contenus publiés par les seeders. Depuis septembre 2023, les opérateurs de Matriochka ont conduit au moins 90 manœuvres successives, au fil desquelles ils ont adapté et testé différentes méthodes visant à diffuser, puis à porter des contenus à la connaissance de leurs cibles.

Fig. 1 : mode opératoire de la campagne Matriochka, illustré par une opération mettant en cause la qualité de l’eau de la Seine

> **[Figure F1 - p. 4] Schéma de mode opératoire avec captures de publications X (Fig. 1)**
>
> Schéma en deux phases et deux chaînes de diffusion parallèles (« Chaîne de diffusion n°1 » et « n°2 »), illustré par une opération visant la qualité de l'eau de la Seine ; toutes les captures datent du 15 avril 2024. Une flèche descend de chaque publication de la phase 1 vers la publication correspondante de la phase 2.
>
> Phase 1 - « publication d'un faux contenu par des comptes seeders » :
> - Chaîne n°1 : compte « lingvetsdermau1976 » (@lingvetsde10091), publication du 15 avril 2024 à 7:35 AM UTC : « The reason for the increased bacteria content in Sena is the sanctions against Russia. » [La cause de la hausse de bactéries dans la Seine (écrit « Sena ») serait les sanctions contre la Russie.] La publication joint une image de faux article au style NBC News, accompagnée d'une capture au style du site de RFI ; des passages sont entourés en bleu.
> - Chaîne n°2 : compte « AlisaPritchett » (@pritchett69486), publication du 15 avril 2024 à 7:49 AM UTC : « Polish components can't cope with the cleaning of the Seine » [Les composants polonais n'arrivent pas à gérer le nettoyage de la Seine]. Même type de montage (faux article au style NBC News et capture au style RFI).
>
> Phase 2 - « mention du post initial par des comptes quoters dans les commentaires de publications des cibles » (publications à 8:00 AM UTC) :
> - Chaîne n°1 : compte « daynisdagus1974 » (@daynisdagu47968), en réponse (« Replying to ») à @EDMO_EUI, texte « The latest news makes me cringe », qui cite la publication de @lingvetsde10091.
> - Chaîne n°2 : compte « lansahoupe1978 » (@lansahoupe67580), en réponse à @OSINTI1, texte « I don't understand how long this is going to go on???? », qui cite la publication de @pritchett69486.
> - Le mot « cibles » est placé entre les deux, avec des flèches pointillées rouges vers les mentions @EDMO_EUI et @OSINTI1 (encadrées en rouge).
>
> Ce que la figure enseigne : le mode opératoire est un tandem seeder / quoter. Les seeders publient le faux (ici un faux article imitant NBC News et RFI, relayant un narratif liant les sanctions contre la Russie à la qualité de l'eau de la Seine), puis des quoters le citent en réponse à une publication d'une cible (ici EDMO et un compte OSINT) avec un texte banal, pour pousser la cible à vérifier. Les seeders publient à quelques minutes d'intervalle (7:35 et 7:49 UTC), les quoters environ 25 minutes plus tard.

<!-- page 5 -->
2.1.1 Schéma de diffusion des contenus Les opérations de Matriochka impliquent généralement deux à trois comptes seeders, qui primo- diffusent sur X les contenus à quelques minutes d’intervalle. Entre 30 à 45 minutes plus tard, deux à trois comptes quoters 3 commencent à partager les posts des comptes seeders, en répondant au dernier 2F

post de la cible. Les quoters ajoutent le plus souvent un texte, un emoji, ou simplement une mention de la cible, ce complément étant unique à chaque opération et quoter. Cette seconde phase s’étend habituellement sur plusieurs heures, avec un intervalle moyen de 45 secondes entre chaque quote.

Si les comptes agissent de manière coordonnée (cf. Fig. 2 ci-dessus), l’intervalle de temps entre chaque publication rend probable leur gestion manuelle, potentiellement par un groupe d’opérateurs humains.

Fig. 2 : chronologie de publication de trois quoters d’une opération de la campagne Matriochka

> **[Figure F2 - p. 5] Graphique (nuage de points / chronologie de publication)**
>
> Chronologie des publications de trois comptes quoters au sein d'une même opération de la campagne, le 1er mai 2024. Axe horizontal : l'heure (graduations 11:00 [May 1, 2024], 11:30, 12:00, 12:30, 13:00, 13:30), axe vertical : une ligne par quoter (trois lignes). Chaque point est un post ; la couleur identifie le quoter, rappelée dans la légende « messages » qui donne le texte (et le lien X) de chaque quoter :
> - jaune : « why did he do it? what did he accomplish? » (lien x.com/Bohyun579659/status/1785589568084783135) ;
> - orange : « what was he thinking when he did that? » (lien x.com/Ben990589695027/status/1785594206729220276) ;
> - bleu : « who's seen this graffiti? » (lien x.com/LachelleNa61051/status/1785593041463435344).
>
> Les publications forment trois salves, lisibles approximativement : une courte salve juste avant et autour de 11:00 (environ 10:50-11:15), une salve dense d'environ 11:40 à 12:35, puis une dernière salve vers 13:35-13:50. Dans chaque salve, les trois comptes publient presque en même temps, à intervalles réguliers de quelques dizaines de secondes.
>
> Ce que la figure enseigne : la seconde phase s'étend sur plusieurs heures avec des posts très rapprochés (intervalle moyen de 45 secondes d'après le texte) de trois comptes qui chacun répètent un message propre. La synchronisation entre les comptes atteste la coordination ; la cadence et ses pauses rendent toutefois probable une gestion manuelle par un groupe d'opérateurs plutôt qu'une automatisation complète.

VIGINUM a identifié à ce titre des erreurs considérées comme peu probables en cas d’automatisation, parmi lesquelles :

- l'utilisation par un quoter du texte d’un autre quoter durant la même opération 4 ; 3F

- l’emploi de tags erronés dans certains posts de quoters 5 ; 4F

- la présence d’erreurs typographiques dans un nombre limité de posts de quoters 6 ; 5F

- l’existence de quotes dans un fil de commentaires, et non en réponse au post de la cible 7. 6F

Par ailleurs, les comptes X impliqués dans la campagne Matriochka sont utilisés pour conduire plusieurs opérations successives, un compte quoter devenant quasi systématiquement seeder, et inversement. À titre d’exemple, le compte @wosuhitsu1972 a été impliqué dans au moins cinq opérations de Matriochka entre le 14 et le 28 mars 2024, tour à tour en tant que quoter, seeder, seeder, quoter puis seeder. Le compte a été depuis suspendu. Au-delà de ce schéma de diffusion, Matriochka a connu plusieurs évolutions et variations depuis son apparition, possiblement pour tester l’efficacité de nouveaux procédés. VIGINUM a notamment identifié la présence de posts traduits automatiquement en langue chinoise 8, la promotion de posts de 7F

seeders par les seeders eux-mêmes, ainsi que des changements dans le profil des comptes impliqués dans la campagne.

Dans de rares cas, les opérations de Matriochka ont impliqué jusqu’à cinq comptes seeders. À titre d’exemple, le quoter @Beth65780671768 semble avoir repris accidentellement le texte du second quoter de l’opération, @Benjamin1563563, dans l’un de ses 97 quotes. Cf. https://perma.cc/A4RY-7NST. À titre d’exemple, le quoter @BritannyJa97256 a taggué @maxhofmann dans les commentaires d’un tweet de @dw_politics, ciblant ainsi la mauvaise entité. Cf. https://archive.ph/KpAm4. Notamment des espaces manquants entre le tag de la cible et le texte du quoter. https://perma.cc/SWY8-5JLZ. https://archive.ph/6lhVf.

<!-- page 6 -->
En outre, entre les mois de septembre 2023 et février 2024, la majorité des comptes seeders et quoters employés semblaient avoir été achetés auprès d’une entreprise spécialisée. Quelques semaines avant qu’ils soient impliqués dans la campagne Matriochka, certains comptes affichaient en effet dans leur pseudonyme leur rattachement à l’entreprise WebMasterMarket, qui commercialise notamment des comptes X 9. Ils partageaient également des caractéristiques communes, dont des liens avec des 8F

personnes d’origine asiatique, des dates de création anciennes, et des posts récents promouvant des actifs chiffrés comme Memecoin. Créés peu de temps avant les opérations, les comptes utilisés possèdent tous des photos de profil générées numériquement, n’indiquent aucune biographie ou localisation, et ne comptent aucun abonné ou abonnement. En l’état, VIGINUM estime que les comptes exploités pour Matriochka pourraient avoir été achetés dans différents pools par les opérateurs de la campagne, voire possiblement compromis 10. 9F

Enfin, plusieurs médias ont indiqué publiquement avoir reçu des courriers électroniques les invitant à prendre connaissance des contenus mis en ligne sur Telegram (cf. section 1.3) ou sur des sites liés à l’écosystème « pravda » 11 du réseau Portal Kombat. En l’état, il semble probable que les opérateurs de 10F

la campagne Matriochka soient à l’origine de ces envois, afin d’attirer l’attention de cibles privilégiées sur les faux contenus 12. 11F

2.1.2 Cibles des campagnes VIGINUM considère que les cibles des campagnes de Matriochka sont les entités dont les publications sont commentées par les comptes liés à la campagne. D’après les données collectées par VIGINUM, chaque opération vise, sur X, entre 50 et 150 entités et individus. La majorité des cibles sont des médias (AFP, BBC, USA Today), des cellules de fact-checking ou de lutte contre la désinformation (EU Disinfo Lab, France 24 – Info ou Intox, FactCheck Bulgaria), ainsi que des personnalités travaillant pour ces organisations ou dans le domaine du fact-checking (Christo GROZEV, Alexandre ALAPHILIPPE, Julian RÖPCKE). VIGINUM a également observé que les activités de Matriochka ont ciblé des universités, des fonds d’investissement, des organisations internationales, des entités gouvernementales et des partis politiques 13. Si les opérateurs de Matriochka mettent régulièrement à jour la liste de leurs cibles, 12F

certaines organisations ou personnalités semblent néanmoins quasi systématiquement visées, en particulier les agences de presse nationales ou internationales. Au total, VIGINUM a recensé plus de 500 comptes X différents, ciblés depuis le début de la campagne, en septembre 2023. Bien que les pays « occidentaux » soient les plus représentés, la campagne Matriochka a également visé des institutions implantées en Ukraine 14, dans les Balkans 15, dans le 13F 14F

Caucase 16, au Proche-Orient 17, en Amérique latine 18, en Asie 19, en Afrique 20, ainsi que des médias issus 15F 16F 17F 18F 19F

de l’opposition russe, bélarusse et iranienne 21. 20F

Cf. https://archive.ph/HLsIe. Les comptes peuvent notamment être achetés via des actifs chiffrés comme Ethereum ou Bitcoin. Cf. notamment les articles de l’AFP (https://archive.ph/2G4ML) et de The New Arab (https://archive.ph/VP5Fg). À titre d’exemple : https://demagog.org.pl/analizy_i_raporty/matriosza-rosyjska-kampania-uderza-w-media-i-fact-checkerow/, https://checkfirst.network/wp-content/uploads/2024/06/Operation_Overload_WEB.pdf. https://checkfirst.network/wp-content/uploads/2024/06/Operation_Overload_WEB.pdf. Dont le parti d’extrême-droite allemand Alternativ für Deutschland (@AfD). Dont des ministères régaliens ukrainiens (@DefenceU, @MVS_UA) et le Center for Countering Disinformation (@CforCD). Dont des comptes kosovares (@HibridInfo), bosniens (@Istinomjer) et macédoniens (@vistinomer). Dont des comptes géorgiens (@MythDetector), arméniens (@CivilNetTV) et azerbaïdjanais (@teyitorg). Dont des comptes turcs (@dogrulukpayicom), syriens (@VeSyria), jordaniens (@misbar_en) et libanais (@AbbassFneish). Dont des comptes mexicains (@NotiPressMX), équatoriens (@ECUADORCHEQUEA) et argentins (@Chequeado). Dont des comptes indiens (@factinkannada), sri-lankais (@VeriteResearch) et philippins (@PressOnePH, @mindanewsdotcom). Dont des comptes burundais (@AntoineKaburahe) et soudanais (@BeamReports). Dont les comptes @antibot4navalny, @ProverenoM, @BelarusFiles et @FactNameh.

<!-- page 7 -->
Parmi les cibles figurent au moins une quarantaine de comptes X français dont des médias et des cellules de fact-checking 22, des personnalités 23 et des entités gouvernementales 24. 21F 22F 23F

VIGINUM a également observé que les opérateurs de Matriochka visent systématiquement plusieurs pays (cf. carte ci-dessous). À l’échelle de chaque pays, les organisations et personnalités ciblées semblent être visées dans le même ordre, ce qui suggère que les opérateurs s’appuient sur une liste prédéfinie. VIGINUM a identifié à ce titre des erreurs de ciblage qui renforcent l’hypothèse d’une gestion manuelle des comptes. Les opérateurs ont notamment ciblé des comptes plusieurs fois d’affilée 25, ainsi que des comptes présentant une proximité orthographique avec la cible supposée 26. 24F 25F

Fig. 3 : origine des cibles de la campagne Matriochka

> **[Figure F3 - p. 7] Carte (planisphère)**
>
> Planisphère où les pays dont des comptes ont été ciblés par la campagne sont colorés en bleu-vert, les autres en gris clair. Aucune légende chiffrée, aucun pays n'est étiqueté ; la lecture ci-dessous porte sur les aplats, et les petits pays ou états proches peuvent être mal identifiés.
>
> Pays ou zones en couleur :
> - Amérique du Nord : États-Unis, Canada, Mexique, Groenland.
> - Amérique du Sud : Brésil, Argentine, Chili, Colombie, Venezuela, Équateur, Bolivie (semble coloriée) ; Pérou, Paraguay, Uruguay et Guyanes apparaissent en gris.
> - Europe : la grande majorité des pays, de l'Islande et de la Scandinavie à la péninsule ibérique, dont France, Royaume-Uni et Irlande, Allemagne, Italie, Pologne, ainsi que la Turquie et une grande partie des Balkans (quelques États, dont la Suisse, la Biélorussie et la Moldavie semblent en gris ou illisibles).
> - Russie.
> - Caucase et Proche / Moyen-Orient : Géorgie, Arménie, Azerbaïdjan, Iran, Irak, Syrie, Liban, Jordanie.
> - Afrique : Maroc, Égypte, Soudan, Nigeria, Burundi.
> - Asie et Océanie : Inde, Sri Lanka, Philippines, Japon, Australie, Nouvelle-Zélande.
>
> Pays en gris (non ciblés d'après la carte) : Chine, Asie centrale, Asie du Sud-Est continentale, péninsule arabique, l'essentiel de l'Afrique subsaharienne, Madagascar.
>
> Ce que la figure enseigne : la campagne cible des comptes dans des dizaines de pays sur tous les continents, bien au-delà des pays occidentaux, selon une même liste prédéfinie par pays ; plus de 500 comptes X différents sont recensés (texte). Le périmètre (fact-checkers locaux, ministères ukrainiens, médias d'opposition russes, bélarusses et iraniens) ne se limite pas à la France.

Dans le cadre de son investigation, VIGINUM a ainsi pu caractériser l’utilisation de procédés artificiels (manœuvres coordonnées entre des comptes inauthentiques) dans le schéma de diffusion de la campagne Matriochka.

Dont TF1, Le Monde, Mediapart, France 24, Le JDD, le collectif Sleeping Giants, ou encore le site d’extrême-droite F de Souche. Dont Jean-Marc MORANDINI et Tristan WALECKX. Notamment la préfecture du Val d’Oise. Cf. https://ghostarchive.org/archive/TSSYx et https://ghostarchive.org/archive/9Q6GW. À l’instar de « lobs » (@unclelobs), un compte ciblé parmi les médias français et probablement confondu avec @Le_NouvelObs. Cf. https://perma.cc/AAX7-SVQU.

<!-- page 8 -->
2.2 Contenus manifestement inexacts ou trompeurs

2.2.1 Des contenus trompeurs et mensongers cherchant à discréditer l’Ukraine et ses alliés La première occurrence de cette campagne semble avoir eu lieu le 5 septembre 2023 27. En l’espèce, 26F

cette première publication a relayé un reportage usurpant l’identité de Fox News en demandant à divers médias de vérifier l’information. Par la suite, la campagne Matriochka a principalement consisté en la diffusion de faux graffitis et de publications usurpant la charte graphique de médias, d’institutions et d’ONG occidentales, selon le mode opératoire susmentionné. À ce stade, ces contenus ont été publiés en langues française, anglaise, italienne, allemande, russe et ukrainienne.

Matriochka diffuse trois types de contenus usurpant l’identité des médias, des institutions et des ONG :

- des reportages vidéos au contenu mensonger reprenant la charte graphique et la police d’écriture de l’organisation. Ces faux reportages auraient été réalisés à l’aide de banques d’images et de musiques libres de droit 28 ; 27F

- de fausses captures d’écran présentant un extrait d’un article, d’une story Instagram ou d’un short YouTube d’un média, d’une organisation ou d’un individu ;

- de faux documents officiels usurpant la charte graphique d’une entité gouvernementale. Les autres publications consistent à diffuser de faux graffitis dans des rues de grandes villes occidentales. Ces graffitis sont le résultat d’un montage entre une photographie d’un lieu bien existant et une caricature ciblant des personnalités ukrainiennes ou européennes. Ces images truquées peuvent également usurper l’identité graphique de certains artistes de rue comme le Français Lekto. Les narratifs véhiculés par ces publications ciblent principalement l’Ukraine et sont destinés à décrédibiliser le gouvernement ukrainien et son président, ou encore à critiquer l’arrivée de réfugiés ukrainiens dans les pays occidentaux. Ainsi, de nombreux faux graffitis, parfois à caractère antisémite, présentent Volodymyr ZELENSKY en mendiant ou en criminel de guerre et plusieurs faux reportages ont présenté défavorablement les réfugiés ukrainiens en Europe 29 (voir ci-dessous). 28F

Fig. 4 : captures d’écran d’un faux graffiti caricaturant Volodymyr ZELENSKY et d’un faux reportage critiquant les réfugiés ukrainiens

> **[Figure F4 - p. 8] Captures de publications X (faux graffiti et faux reportage)**
>
> Deux publications X diffusées par des comptes de la campagne, reproduites comme exemples de faux contenus anti-ukrainiens.
>
> À gauche : compte « Käthe » (@sumanadevi97142), 25 décembre 2023, 9:20 AM, 1 458 vues. Texte : « Graffiti with Zelensky in the Skid Row area in Los Angeles. Skid Row is known as one of the largest populations of drug addicts and homeless people in the United States. » [Graffiti avec Zelensky dans le quartier de Skid Row à Los Angeles. Skid Row est connu comme l'une des plus grandes populations de toxicomanes et de sans-abri des États-Unis.] Image : photo d'un mur blanc de brique avec, peint dessus, une caricature d'un homme assis à terre drapé dans un drapeau ukrainien, la main tendue en mendiant. Le rapport explique qu'il s'agit d'un montage d'une photographie d'un lieu réel et d'une caricature.
>
> À droite : compte « DeniseWilliams » (@DeniseWill5123), 17 avril 2024, 1:52 PM, 760 vues. Texte : « Ukrainian refugee in Italy sold cats disguised as clones » suivi de trois émojis rieurs [Un réfugié ukrainien en Italie a vendu des chats déguisés en clones]. Vidéo (0:03 / 1:45) avec le logo « euronews. » en haut à droite et le sous-titre incrusté « A Ukrainian refugee received about 900,000 euros from Italians » [Un réfugié ukrainien a reçu environ 900 000 euros d'Italiens] sur une place d'une ville italienne ; le rapport indique que ce faux reportage usurpe l'identité d'Euronews.
>
> Ce que la figure enseigne : deux formats de contenu de la campagne : le faux graffiti (montage) contre le président ukrainien, et le faux reportage vidéo imitant la charte d'un média pour présenter défavorablement les réfugiés ukrainiens. Les deux publications sont reprises par des comptes aux noms et pseudonymes typiques des comptes de la campagne, avec peu de vues.

https://archive.ph/4K6Oj. Cf. article du média russe Provereno : https://provereno.media/blog/2024/05/11/krysy-klopy-tuberkulyoz-i-finansovye-problemy- feyki-rossyskoy-propagandy-ob-olimpiade-v-parizhe/. Cf. archive du faux graffiti de Volodymyr ZELENSKY (https://archive.ph/VEFVh) et du faux reportage usurpant l’identité d’Euronews (https://archive.ph/XMWb6).

<!-- page 9 -->
2.2.2 La France, une cible privilégiée de la campagne Matriochka Une autre partie des narratifs est aussi destinée à discréditer les gouvernements européens, notamment leur politique de soutien au profit de l’Ukraine. À ce titre, la France constitue une cible privilégiée pour les opérateurs de la campagne. En effet, les médias BFMTV, Le Parisien, Libération, Le Monde et La Montagne, ainsi que la Banque de France, la ville de Paris et la DGSI, ont été victimes d’usurpations d’identité dans le cadre de la campagne Matriochka. En outre, plusieurs faux graffitis publiés étaient présentés comme situés en région parisienne. Ces contenus contrefaits ciblant la France ont relayé plusieurs narratifs visant notamment à créer une défiance envers les institutions Fig. 5 : capture d’écran d’un faux graffiti caricaturant Emmanuel MACRON françaises et les membres du gouvernement 30. 29F

> **[Figure F5 - p. 9] Capture de publication X (faux graffiti ciblant Emmanuel Macron)**
>
> Publication X du compte « Mala Vika » (@MalaVik96720930), 27 mars 2024, 10:45 AM, 1 110 vues. Texte : « A French artist with the pseudonym Sucr 128 created an accurate allusion to Macron's politics. » [Un artiste français sous le pseudonyme Sucr 128 a créé une allusion juste à la politique de Macron.]
>
> Image jointe, en deux parties : à gauche, photo d'un mur blanc portant un graffiti légendé « LA POLITIQUE DE MACRON » qui montre une silhouette caricaturée à vélo, avec en bas du mur une autre silhouette allongée au sol et une bulle de texte (semble lire « C'est la faute à Poutine ! ») ; à droite, un encart imitant un lien de presse, en-tête « Les murs parlants à Paris : », titre « L'artiste de street art Sucr 128 a réalisé un graffiti avec Emmanuel Macron » et domaine « L.LEPARISIEN.FR » ; au-dessus, une vue en gros plan du même mur.
>
> Ce que la figure enseigne : le graffiti est un montage faussement attribué à un artiste de rue et à une rubrique de Le Parisien (usurpation de média français, un des cas listés en annexe 3.2), placé en région parisienne pour viser la politique de soutien à l'Ukraine et le Président de la République. Le contenu est repris par un compte inauthentique à très faible audience (1 110 vues).

Par ailleurs, VIGINUM a observé que de nombreuses manœuvres de la campagne Matriochka ont ciblé l’organisation des Jeux olympiques et paralympiques 2024 (JOP24). Ces manœuvres informationnelles ont usurpé l’identité de médias et d’institutions dans le but de diffuser l’idée, auprès d’audiences aussi bien françaises qu’internationales, selon laquelle les JOP24 seront un échec. Ainsi, des publications ont notamment affirmé, en usurpant la charte graphique de la CIA 31, que le risque terroriste était trop grand 30F

pour le bon déroulement des évènements, ou encore diffusé un faux document de la Mairie de Paris pour inviter les Parisiens à ne pas activer leur climatisation, car celle-ci émettrait des ondes qui pourraient perturber les drones sécurisant les infrastructures des JOP24 (voir ci-dessous).

Fig. 6 : capture d’écran de publications ciblant les JOP24

> **[Figure F6 - p. 9] Captures de publications X (contenus visant les JOP24)**
>
> Deux publications X diffusant de faux documents sur les Jeux olympiques et paralympiques de Paris 2024.
>
> À gauche : compte au nom affiché en caractères thaïs (@m7w8rYlG7Xxqq1B), 15 février 2024, 4:36 PM. Texte : « The CIA says the terrorist threat level for the 2024 Olympics in Paris is very HIGH. » [La CIA affirme que le niveau de menace terroriste pour les JO 2024 à Paris est très ÉLEVÉ.] Vidéo (0:03 / 1:18) sur fond noir avec le texte « THE TERRORISM THREAT LEVEL FOR THE 2024 OLYMPIC GAMES IN PARIS IS ASSESSED AS HIGH » et le sceau de la Central Intelligence Agency (usurpation de la charte de la CIA). Le nombre de vues est partiellement coupé sur cette capture (« 76 Views » lisible sur la reprise du même visuel dans le rapport JOP24).
>
> À droite : compte « Erika Darcie » (@EDarcie72771), 8 mai 2024, 11:58 AM, 673 vues. Texte : « Paris residents asked not to use air conditioners from July 20 to August 15, 2024, probably because the Paris Olympics security system mistook air conditioners for drones. » [Les Parisiens priés de ne pas utiliser la climatisation du 20 juillet au 15 août 2024, probablement parce que le système de sécurité des JO a pris les climatiseurs pour des drones.] Image composite de deux faux documents : un communiqué à l'en-tête de la Ville de Paris (titre « Il est recommandé aux Parisiens de ne pas utiliser les systèmes de climatisation », corps du texte trop petit pour être transcrit, mentionnant semble-t-il la période du 20 juillet au 15 août 2024), et une page de presse, rubrique « SOCIÉTÉ », avec la photo de personnes près d'une fontaine devant la pyramide du Louvre, la phrase incrustée « IL EST RECOMMANDÉ AUX PARISIENS DE NE PAS UTILISER LES SYSTÈMES DE CLIMATISATION » et un encart d'un journal anglophone (logo partiellement lisible) titré « Paris Olympics defence system mistakes air conditioners for drones » avec une photo des anneaux olympiques devant la tour Eiffel.
>
> Ce que la figure enseigne : les contenus visant les JOP24 usurpent des institutions (CIA, Mairie de Paris) et des médias pour installer l'idée que les JOP24 seront un échec (menace terroriste, brouillage des drones de sécurité), pour des publics français et internationaux. Ils reprennent le format seeder : capture d'écran de faux document attachée à un message court.

À titre d’exemple, les opérateurs de Matriochka ont relayé des narratifs présentant le Président de la République comme responsable d’une escalade des tensions avec la Russie (https://archive.ph/1Lx92), ou accusant les forces de l’ordre françaises d’agressions sexuelles durant les tensions en Nouvelle-Calédonie (https://archive.ph/ZqhAG). https://blogs.microsoft.com/on-the-issues/2024/06/02/russia-cyber-bots-disinformation-2024-paris-olympics/.

<!-- page 10 -->
Enfin, le 7 février 2024, la campagne Matriochka s’est illustrée par la diffusion d’une vidéo présentée comme un montage réalisé par l’organisation étudiante française d’extrême-droite Groupe union défense (GUD), qui menaçait de « brûler la Banque de France » car cette dernière « mentirait » sur sa solvabilité (voir ci-dessous). Cette vidéo sous fausse bannière s’inscrivait dans une manœuvre plus large visant à discréditer la Banque de France par l’usurpation de son identité ainsi que celle de la DGSI afin d’inviter la population à retirer massivement des liquidités.

Fig. 7 : captures d’écran des publications ciblant la Banque de France

> **[Figure F7 - p. 10] Captures de publications X (manœuvre contre la Banque de France)**
>
> Trois publications X (et leurs vidéos) de la manœuvre visant à discréditer la Banque de France, présentées dans l'ordre de lecture du document.
>
> 1. Compte « Cierra Craig » (@cierra33586), 2 février 2024, 5:11 PM, 789 vues. Texte : « When any government says not to do something, it MUST be done! especially if it's your money. withdraw it » [Quand un gouvernement dit de ne pas faire quelque chose, il FAUT le faire ! surtout si c'est votre argent. retirez-le]. Vidéo (0:04 / 0:44), fond bleu, pastille ronde en haut à gauche (semble un sceau institutionnel), texte « La DGSI avertit que les informations sur la crise bancaire en France sont fausses » et enseigne « LA BANQUE POSTALE » : usurpation de l'identité de la DGSI.
>
> 2. Compte « India Gonsalves » (@GonsalvesI76113), 7 février 2024, 6:35 PM, 326 vues, 1 repost. Texte : « Bank of France! Stop lying! We know you're in big trouble! Give people their money back! Otherwise we'll burn you down! » (guillemet anglais fermant visible dans la capture) [Banque de France ! Arrêtez de mentir ! Nous savons que vous avez de gros ennuis ! Rendez aux gens leur argent ! Sinon nous vous ferons brûler.] Vidéo (0:28 / 0:33) sur fond de flammes avec le nom « GROUPE UNION DÉFENSE » : fausse bannière attribuée au GUD.
>
> 3. Compte « Kabilash Choudhary » (@KabilashChoudh1), 17 janvier 2024, 2:05 PM, 54 vues. Texte : « @BFMTV French media spreading panic around cash withdrawals. The French Central Bank has made a video and urges French people not to withdraw cash. @banquedefrance » [Les médias français sèment la panique autour des retraits d'espèces. La Banque de France a fait une vidéo et exhorte les Français à ne pas retirer d'argent.] Vidéo verticale (0:02 / 0:41), interface de type vidéo courte avec le compte « @banquedefrance » et un bouton « S'abonner », en-tête « BANQUE DE FRANCE EUROSYSTÈME » et question « Pourquoi est-il préférable d'utiliser la monnaie scripturale ? » avec la légende « La monnaie scripturale soutient l'économie de la France ».
>
> Ce que la figure enseigne : une même thématique a été déclinée en trois contenus d'identités usurpées différentes (DGSI, GUD, Banque de France, avec mention de BFMTV), du 17 janvier au 7 février 2024, pour semer la défiance dans le système bancaire et inciter à retirer des liquidités. L'audience reste faible (de 54 à 789 vues). Le texte situe la vidéo GUD le 7 février 2024 et la qualifie de vidéo sous fausse bannière.

Dans le cadre des analyses réalisées, VIGINUM a ainsi pu caractériser la diffusion d’allégations fausses ou inexactes visant à tromper les internautes.

<!-- page 11 -->
2.3 Implication, directe ou indirecte, d’un acteur étranger Les investigations de VIGINUM ont permis de confirmer que les contenus publiés sur X sont diffusés en amont par des chaînes Telegram russophones (cf. annexe 3.3). À une exception près 32, les reportages, 31F

captures d’écran et graffitis contrefaits semblent avoir été primo-diffusés par des chaînes telles que @sheyhtamir1974, @belshkarvka et @thehandofkremlin, et mis en ligne dans un intervalle de temps relativement restreint. Ces éléments suggèrent que les contenus ont été élaborés initialement pour des audiences russophones. Si les administrateurs des trois chaînes susmentionnées semblent différents et possèdent des lignes éditoriales propres, les publications originales en langue russe, associées à des contenus trompeurs, ont souvent fait l’objet de copy-pasta 33, notamment identifiables à partir d’entêtes spécifiques. 32F

L’analyse sémantique des messages publiés entre le 22 février 2022 et le 10 mai 2024 par les chaînes @sheyhtamir1974, @belshkarvka et @thehandofkremlin, et comprenant au moins l’une de ces deux entêtes en langue russe : « #нам_пишут_наши_любимые_подписчики » (« #nos abonnés préférés nous ont écrit ») et « #от подписчика » (« #de la part d’un abonné »), a démontré la fréquence d’utilisation de la technique du copy-pasta par ces trois chaînes, et l’augmentation du nombre de publications de contenus contrefaits depuis septembre 2023.

Fig. 8 : graphique des copy-pasta des messages trompeurs publiés sur les chaînes Telegram

> **[Figure F8 - p. 11] Graphique (histogramme groupé par mois)**
>
> Histogramme groupé : axe vertical « messages dupliqués » (graduations 0, 10, 20, 30) ; axe horizontal : mois, graduations Jul 2022, Oct 2022, Jan 2023, Apr 2023, Jul 2023, Oct 2023, Jan 2024, Apr 2024. Trois séries, une par chaîne Telegram : orange @belshkarvka, bleu @sheyhtamir1974, jaune @thehandofkremlin. Chaque barre compte les messages utilisant le procédé de copy-pasta dans le mois, repérés par les entêtes russes « #нам_пишут_наши_любимые_подписчики » et « #от подписчика » (analyse des messages entre le 22 février 2022 et le 10 mai 2024). Les valeurs ci-dessous sont lues graphiquement (approximatives).
>
> - De juillet 2022 à août 2023 : faible niveau, de 0 à 8 par chaîne et par mois (par exemple environ 5 / 1 / 4 en juillet 2022 pour belshkarvka / sheyhtamir1974 / thehandofkremlin, jusqu'à environ 8 et 7 pour les deux chaînes principales en décembre 2022), avec des mois à zéro au printemps 2023. @sheyhtamir1974 reste le plus bas (0 à 3).
> - À partir de septembre 2023 : hausse nette et durable. Environ 6 / 2 / 6 en septembre 2023, 13 / 3 / 13 en octobre 2023, 17 / 4 / 15 en novembre 2023.
> - Plateau haut de décembre 2023 à avril 2024 : @belshkarvka entre 19 et 23 par mois, @thehandofkremlin entre 9 et 21, @sheyhtamir1974 entre 6 et 13.
> - Pic, dans le dernier groupe de barres complet vers avril-mai 2024 : @belshkarvka environ 36, @sheyhtamir1974 environ 15, @thehandofkremlin environ 13 ; deux barres plus basses suivent (environ 9 / 1 / 7).
>
> Ce que la figure enseigne : les trois chaînes publient en copy-pasta les mêmes contenus trompeurs, et ce volume décolle à partir de septembre 2023, date du début de la campagne Matriochka sur X ; la chaîne @belshkarvka est la plus active. Le décalage entre les chaînes et leur synchronisme soutiennent l'hypothèse d'une primo-diffusion coordonnée sur Telegram (contenus d'abord destinés à l'audience russophone).

Au regard de ces éléments, VIGINUM formule l’hypothèse que ces contenus ont été élaborés par des tiers afin d’être diffusés de manière coordonnée sur Telegram. Aucun élément technique ne permet pour l’heure de lier le mode opératoire Matriochka aux tiers susceptibles d’avoir conçu les contenus diffusés par les chaînes. L’hypothèse de l’implication de tiers semble renforcée par le fait que les administrateurs de ces chaînes sont susceptibles d’avoir fait l’objet d’une rémunération pour avoir publié des « contenus politiques », une pratique courante dans l’écosystème des chaînes pro-Kremlin, selon une enquête du média d’investigation russe Proekt 34. À titre d’exemple, la chaîne @thehandofkremlin invite à contacter un 33F

intermédiaire, dénommé « Sergey KALACHNIKOV », pour toutes ses collaborations commerciales. Ce dernier présente en outre une chaîne Telegram 35 en langue russe, sur laquelle il propose de publier des 34F

contenus contre rémunération (voir ci-dessous).

Enfin, la campagne RRN a déjà publié de faux reportages fortement similaires à ceux publiés par Matriochka lors de précédentes manœuvres 36, permettant de formuler l’hypothèse que ces contenus 35F

https://archive.ph/PHjTq. Bloc de texte ou de visuel copié-collé à l'identique ou presque, sur une ou plusieurs plateformes web, dans le but d'amplifier la visibilité d'un message. https://www.proekt.media/narrative/telegram-kanaly/. https://t.me/together_to_the_stars. Ainsi, le 18 et le 19 mars 2023, un contenu sponsorisé de la campagne RRN a diffusé sur Facebook une vidéo usurpant l’identité du Figaro affirmant que Nord Stream aurait été saboté par les États-Unis et le Royaume-Uni (ID de la publicité : 4208250569399286). Le 21 octobre 2023, un réseau de comptes X désormais suspendus de la campagne RRN, a publié un reportage usurpant également l’identité du Figaro affirmant qu’Israël utiliserait de fausses vidéos pour justifier son intervention dans la bande de Gaza (https://twitter.com/casusbellii/status/1716429778956189928).

<!-- page 12 -->
ont pu être élaborés par les mêmes acteurs. Cette hypothèse est renforcée par le fait que, d’après des documents publiés par le Washington Post 37, la chaîne Telegram @sheyhtamir1974 apparaît à plusieurs 36F

reprises sur des tableaux de bord d’un certain « Centre S » 38. D’après cet article, ce centre pourrait avoir 37F

des liens avec l’Administration présidentielle russe et serait chargé de coordonner des « opérations d’influence » vers l’étranger. Par ailleurs, toujours selon le Washington Post, l’Administration présidentielle russe est soupçonnée d’avoir sous-traité une partie de ses actions à ASP et Struktura, les deux entreprises ayant été publiquement accusées d’être derrière la campagne RRN 39. 38F

Ainsi, l’utilisation systématique de chaînes russophones en tant que primo-diffuseur ainsi que la publication de contenus originaux en langue russe constituent un faisceau d’indices concordants, suggérant l’implication d’un acteur étranger.

Fig. 9 : capture d’écran d’une publication de la chaîne Telegram @together_to_the_stars liée à Sergey KALACHNIKOV et proposant des partenariats avec des chaînes Telegram contre rémunération

> **[Figure F9 - p. 12] Capture de publication Telegram (tarifs de publicité de chaînes)**
>
> Publication de la chaîne Telegram @together_to_the_stars (liée, selon VIGINUM, à l'intermédiaire « Sergey KALACHNIKOV »), en russe, présentée en deux colonnes. Elle propose de publier de la publicité dans des chaînes Telegram contre rémunération.
>
> Texte d'en-tête [traduit] : « Сотрудничество, реклама » [Coopération, publicité] ; « По взаимному пиару с этими каналами или покупке рекламы пишите мне в ЛС @Sergey3341 » [Pour un échange de promotion avec ces chaînes ou pour acheter de la publicité, écrivez-moi en message privé @Sergey3341] ; « Большинство не удаляет рекламу из ленты. Кто удаляет помечены 1/24 » [La plupart ne retirent pas la publicité du fil. Ceux qui la retirent sont marqués 1/24].
>
> Suit une liste de noms de chaînes avec un prix (le symbole ₽ [rouble] apparaît sur certaines lignes) et parfois un format (1/24, 1/48, 2/24ч : durée de présence en tête de fil / durée avant suppression, en heures, lecture probable) :
> - Colonne de gauche : Пономарь 10 000 (1/24ч) ; Собственный корреспондент 10 000 (1/24ч) ; Синяя Z Борода 7 500 (« 30м в топе, навсегда в ленте » [30 min en tête, définitivement dans le fil]) ; Оптимистка в штатском 5 500 (1/24 ; « Можно и без удаления, будет 10000 ₽, топ 3 часа » [possible aussi sans suppression, 10 000 ₽, 3 h en tête]) ; Хрюн Моржов 5 000 ₽ ; Дед, продай ружье 1/24 - 4 000 ₽, 1/48 - 4 500 ₽, 6 000 sans suppression ; pokusala 4 000 ; Лохматый Z Николаев 5 000 ; ХтоШо | Сергей Черкасский 5 000 ; Цифровая Сатира 7 000 ; Zanoza 3 300 (1/48).
> - Colonne de droite (extraits) : V Рука Кремля Z (« Main du Kremlin ») 3 000 ; СРОЧНО И ТОЧНО 4 000 ; PAVLOVA|| НОВОСТИ 3 000 ; Политический Юмор 2 500 ; Ольгерд Семёнов 2 000 ; Hard News 2 000 ; Урал ZV Дейли 2 000 ; Врач будущего 1 600 ; Операция Z 2 400 ₽ (30/48) ; Правда 2022 1 500 ; follyflly 1 500 ; V тылу | Za лентой 1 500 (1/48) ; Информатор Z 1 200, avec la mention « оплата на криптокошелёк » [paiement sur portefeuille crypto] ; Комната высмеха и юмора 1 000 (2/24ч) ; Лис Z Ворон 1 000 (1/24) ; Телега Новости 1 000 ; СССР ТВ 2 000 (1/24) ; РУССКИЙ ПАТРИОТ 3 500 ; ROSGVARDIA_RUSSIA 2 700 (1/24) ; По секрету вам скажу 700 ; СВО ZOV 500 ; Анекдот Тодкена 1 500 ; Это Позитив! 400 ; Россия|Новости|Важно 400 ; Не может быть или может 300 ; ANTISEPTIC 1 000. Le nom « Рука Кремля » correspond probablement à @thehandofkremlin.
>
> Ce que la figure enseigne : preuve d'un marché de publication payante dans l'écosystème des chaînes Telegram pro-Kremlin, avec tarifs de 300 à 10 000 (unité monétaire non précisée sauf ₽) par publication et paiement possible en cryptomonnaie. Elle appuie l'hypothèse de VIGINUM, selon l'enquête de Proekt citée, que les administrateurs des chaînes de primo-diffusion peuvent être rémunérés pour publier des « contenus politiques » conçus par des tiers.

https://www.washingtonpost.com/world/2024/02/16/russian-disinformation-zelensky-zaluzhny. https://archive.ph/y1UvB, https://archive.ph/YFeUX et https://archive.ph/B5gDP. Cf. https://about.fb.com/news/2022/11/metas-adversarial-threat-report-q3-2022/.

<!-- page 13 -->
2.4 Atteinte aux intérêts fondamentaux de la Nation VIGINUM estime que l’objectif principal de la campagne Matriochka est de décrédibiliser les personnalités et médias occidentaux engagés dans du fact-checking, y compris publiquement s’ils réagissent aux contenus contrefaits. À ce stade, la recherche d’audience ne semble être qu’un objectif secondaire des opérateurs, comme en atteste la faible volumétrie d’audience et d’engagement aux publications de la campagne 40, et bien que le mode opératoire ait été employé contre plusieurs 39F

centaines d’entités. L’analyse des primo-diffuseurs suggère que des contenus destinés initialement à l’audience russe sont récupérés par les opérateurs de Matriochka, puis réemployés à moindre coût contre des cibles « occidentales ». Dans certains cas, des médias ont effectivement répondu aux appels des quoters, ou même analysé et publié sur les contenus diffusés durant la campagne, s’exposant ensuite à un ciblage massif visant à saturer leur capacité d’investigation. VIGINUM considère cette campagne comme portant atteinte à la réputation des médias traditionnels et des institutions officielles françaises. En effet, depuis le début de l’invasion à grande échelle de l’Ukraine en février 2022, le dispositif d’influence russe prend régulièrement pour cible des fact- checkers 41, et déploie des efforts importants pour discréditer les analyses de médias occidentaux. 40F

Par ailleurs, l’exploitation de l’image du Président de la République à des fins de désinformation ainsi que les attaques répétées contre les JOP2024 et la politique française de soutien à l’Ukraine ont probablement pour finalité d’atteindre la réputation de la France sur la scène internationale, auprès d’audiences choisies. À l’issue de cette analyse, VIGINUM considère que la campagne Matriochka est susceptible de porter directement atteinte aux intérêts fondamentaux de la Nation.

***

Au regard des méthodes de diffusion coordonnées et inauthentiques, de la nature trompeuse et mensongère des publications de Matriochka, des liens probables entre la campagne et des acteurs russes ainsi que l’intention manifeste de porter atteinte à l’image de la France, les critères d’une ingérence numérique étrangère apparaissent réunis. VIGINUM considère par ailleurs que son mode opératoire pourrait évoluer durant les mois à venir pour améliorer la furtivité de ses procédés, piéger un plus grand nombre de cibles, ou atteindre une audience plus large.

À titre d’exemple, l’opération visant à diffuser un faux graffiti de Lekto vers 162 comptes X, lancée le 24 avril 2024, ne comptait que 720 vues en date du 2 mai 2024. Voir notamment : https://dfrlab.org/2022/05/04/how-russia-employs-fake-fact-checking-in-its-disinformation-arsenal.

<!-- page 14 -->
3. ANNEXES

3.1 Tactiques, techniques & procédures [TA01] Plan Strategy - [T0084.002] Plagiarise Content

> **[Figure F10 - p. 14] Tableau (liste TTP DISARM, remise dans l'ordre de lecture)**
>
> Le texte extrait de cette page mélange les deux colonnes de la liste ; voici la liste des tactiques (TA) et techniques (T) DISARM telle qu'elle apparaît, colonne de gauche puis colonne de droite.
>
> Colonne de gauche :
> - [TA01] Plan Strategy : [T0074] Determine Strategic Ends
> - [TA02] Plan Objectives : [T0002] Facilitate State Propaganda ; [T0066] Degrade Adversary ; [T0075] Dismiss ; [T0075.001] Discredit Credible Sources ; [T0076] Distort ; [T0077] Distract ; [T0079] Divide
> - [TA13] Target Audience Analysis : [T0072] Segment Audiences ; [T0072.001] Geographic Segmentation ; [T0081] Identify Social and Technical Vulnerabilities ; [T0081.003] Identify Existing Prejudices ; [T0081.004] Identify Existing Fissures ; [T0081.005] Identify Existing Conspiracy Narratives/Suspicions ; [T0081.008] Identify Media System Vulnerabilities
> - [TA14] Develop Narratives : [T0003] Leverage Existing Narratives ; [T0004] Develop Competing Narratives ; [T0022] Leverage Conspiracy Theory Narratives ; [T0022.001] Amplify Existing Conspiracy Theory Narratives ; [T0022.002] Develop Original Conspiracy Theory Narratives ; [T0040] Demand Insurmountable Proof ; [T0068] Respond to Breaking News Event or Active Crisis ; [T0083] Integrate Target Audience Vulnerabilities into Narrative
> - [TA06] Develop Content : [T0019] Generate Information Pollution ; [T0023] Distort Facts ; [T0023.001] Reframe Context ; [T0023.002] Edit Open-Source Content ; [T0084] Reuse Existing Content
>
> Colonne de droite :
> - (suite de [TA06] Develop Content) : [T0084.002] Plagiarise Content ; [T0084.003] Deceptively Labelled or Translated ; [T0086] Develop Image-Based Content ; [T0086.003] Deceptively Edit Images (Cheap Fakes) ; [T0087] Develop Video-Based Content ; [T0087.002] Deceptively Edit Video (Cheap Fakes)
> - [TA15] Establish Social Assets : [T0090] Create Inauthentic Accounts ; [T0090.001] Create Anonymous Accounts
> - [TA16] Establish Legitimacy : [T0099] Impersonate Existing Entity ; [T0099.002] Spoof/Parody Account/Site ; [T0099.003] Impersonate Existing Organisation ; [T0099.004] Impersonate Existing Media Outlet ; [T0099.005] Impersonate Existing Official ; [T0099.006] Impersonate Existing Influencer
> - [TA07] Select Channels and Affordances : [T0104] Social Networks ; [T0104.001] Mainstream Social Networks ; [T0112] Email
> - [TA09] Deliver Content : [T0115] Post Content ; [T0116] Comment or Reply on Content
> - [TA11] Persist in the Information Environment : [T0128] Conceal Information Assets ; [T0128.001] Use Pseudonyms ; [T0128.004] Launder Information Assets ; [T0129] Conceal Operational Activity ; [T0129.002] Generate Content Unrelated to Narrative
>
> Ce que la figure enseigne : le profil TTP DISARM de Matriochka : usurpation de médias, d'officiels et d'organisations (T0099.003, .004, .005), cheap fakes d'images et de vidéos (T0086.003, T0087.002), comptes inauthentiques anonymes (T0090.001), diffusion par réponses et commentaires sur les réseaux (T0115, T0116) et par e-mail (T0112), réemploi de contenu (T0084), discrédit de sources crédibles (T0075.001).

- [T0074] Determine Strategic Ends - [T0084.003] Deceptively Labelled or Translated [TA02] Plan Objectives - [T0086] Develop Image-Based Content

- [T0002] Facilitate State Propaganda - [T0086.003] Deceptively Edit Images

- [T0066] Degrade Adversary (Cheap Fakes)

- [T0075] Dismiss - [T0087] Develop Video-Based Content

- [T0075.001] Discredit Credible Sources - [T0087.002] Deceptively Edit Video

- [T0076] Distort (Cheap Fakes)

- [T0077] Distract

- [T0079] Divide [TA15] Establish Social Assets

- [T0090] Create Inauthentic Accounts [TA13] Target Audience Analysis - [T0090.001] Create Anonymous

- [T0072] Segment Audiences Accounts

- [T0072.001] Geographic Segmentation

- [T0081] Identify Social and Technical [TA16] Establish Legitimacy Vulnerabilities - [T0099] Impersonate Existing Entity

- [T0081.003] Identify Existing Prejudices - [T0099.002] Spoof/Parody Account/Site

- [T0081.004] Identify Existing Fissures - [T0099.003] Impersonate Existing

- [T0081.005] Identify Existing Conspiracy Organisation Narratives/Suspicions - [T0099.004] Impersonate Existing Media

- [T0081.008] Identify Media System Outlet Vulnerabilities - [T0099.005] Impersonate Existing Official [TA14] Develop Narratives - [T0099.006] Impersonate Existing

- [T0003] Leverage Existing Narratives Influencer

- [T0004] Develop Competing Narratives

- [T0022] Leverage Conspiracy Theory [TA07] Select Channels and Affordances Narratives - [T0104] Social Networks

- [T0022.001] Amplify Existing Conspiracy - [T0104.001] Mainstream Social Theory Narratives Networks

- [T0022.002] Develop Original - [T0112] Email Conspiracy Theory Narratives

- [T0040] Demand Insurmountable Proof [TA09] Deliver Content

- [T0068] Respond to Breaking News - [T0115] Post Content Event or Active Crisis - [T0116] Comment or Reply on Content

- [T0083] Integrate Target Audience Vulnerabilities into Narrative [TA11] Persist in the Information Environment

- [T0128] Conceal Information Assets [TA06] Develop Content - [T0128.001] Use Pseudonyms

- [T0019] Generate Information Pollution - [T0128.004] Launder Information Assets

- [T0023] Distort Facts - [T0129] Conceal Operational Activity

- [T0023.001] Reframe Context - [T0129.002] Generate Content

- [T0023.002] Edit Open-Source Content Unrelated to Narrative

- [T0084] Reuse Existing Content

<!-- page 15 -->
3.2 Entités & médias français dont l’identité a été usurpée

Média Archives en ligne 42 41F Date Le Parisien https://archive.ph/8kQmd 21/09/2023 La Montagne https://archive.ph/oCZmb 09/10/2023 Le Monde https://archive.ph/pefZ7 16/11/2023 RFI https://archive.ph/9I6ZG 24/11/2023 DGSI https://archive.ph/vFEsk 02/02/2024 Figaro https://archive.ph/0d6l3 08/03/2024 France 24 https://archive.ph/PdIVl 28/03/2024 France 24 https://archive.ph/1SW5K 11/04/2024 Figaro https://archive.ph/MndmD 12/04/2024 RFI https://archive.ph/rta16 15/04/2024 RFI https://archive.ph/sVNOk 18/04/2024 RFI https://archive.ph/WvAl3 19/04/2024 BFMTV https://archive.ph/kTtES 08/05/2024 BFMTV https://archive.ph/Wb5VG 08/05/2024 Ville de Paris https://archive.ph/N2OzX 08/05/2024 Libération https://archive.ph/biAIN 11/05/2024 BFMTV https://archive.ph/NGyE8 13/05/2024

3.3 Exemples de diffusion des contenus de la campagne

Chaîne Telegram de primo- Date de Comptes seeder Date de Contenu diffusion diffusion Matriochka sur X diffusion Reportage du Spiegel sur un 28/04/2024 30/04/2024 https://archive.is/a0R2H https://archive.is/dkk0k gardien de à 19h01 à 12h30 but Reportage de 11/05/2024 13/05/2024 BFM TV sur https://archive.is/w6ejq https://archive.is/1GmF8 à 10h07 à 12h39 l’Eurovision Graffitis 10/05/2024 15/05/2024 antisémites à https://archive.is/P2nMD https://archive.is/hAH62 à 15h37 à 13h34 Paris

Archives communiquées par le collectif « antibot4navalny ».

<!-- page 16 -->
À PROPOS DE VIGINUM

Créé le 13 juillet 2021 et rattaché au SGDSN, le service de vigilance et de protection contre les ingérences numériques étrangères (VIGINUM) a pour raison d’être la protection du débat public numérique touchant aux intérêts fondamentaux de la Nation.

Ce service technique et opérationnel de l’État a pour mission de détecter et caractériser les campagnes de manipulation de l’information sur les plateformes numériques, impliquant des acteurs étrangers dans le but de nuire à la France et à ses intérêts.

Service de vigilance et protection contre les ingérences numériques étrangères | SGDSN

Crédit photo couverture : flickr | www.CGPGrey.com ; photo coupée et passage en noir et blanc
