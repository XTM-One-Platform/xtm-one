# Définitions et objectifs du concept de « mode opératoire informationnel » (MOI) (méthodologie VIGINUM, janvier 2026)

Source : site internet du secrétariat général de la défense et de la sécurité nationale (SGDSN), https://www.sgdsn.gouv.fr/viginum/publications/definitions-et-objectifs-du-concept-de-mode-operatoire-informationnel-moi  
Document original : https://www.sgdsn.gouv.fr/files/files/viginum/Publications/20260122_NP_TLP-CLEAR_SGDSN_VIGINUM_MOI.pdf

> Document enrichi pour la base de connaissances : le texte du PDF est complété, à l'emplacement de chaque figure, par une annotation qui décrit son contenu (entités, relations orientées, valeurs) et la règle qu'elle illustre. Les blocs « Figure » sont des descriptions rédigées à partir des images, pas du texte original.

> Note : Note doctrinale de 14 pages (TLP:CLEAR). Le MOI (en anglais « Information Manipulation Set », IMS) est l'équivalent informationnel du mode opératoire d'attaque (« Intrusion Set ») de la CTI. Deux conditions cumulatives le définissent : la clandestinité et la coordination. Le document ne contient que cinq visuels informatifs : un schéma des cinq niveaux (F1), deux jeux de captures de publications (F2, F3), un schéma de diffusion de Storm-1516 (F4) et des captures de l'outil AIMS (F5) ; la page 4 est presque vide.

<!-- page 1 -->
TLP:CLEAR

<!-- page 2 -->
TLP:CLEAR

Sommaire

1. Introduction ..................................................................................................................................................... 3

2. Définitions ........................................................................................................................................................ 5 2.1 Les dimensions de la manipulation de l’information ................................................................................. 5

2.2 Définition d’un « mode opératoire informationnel » ................................................................................ 5 2.3 Principales caractéristiques d’un MOI .......................................................................................................... 6

3. Exemples ........................................................................................................................................................... 9

3.1 Spamouflage ....................................................................................................................................................... 9

3.2 Activités clandestines liées au Baku Initiative Group ................................................................................. 9

3.3 Storm-1516, CopyCop et Lakhta ................................................................................................................... 10

3.4 « Team Jorge » .................................................................................................................................................. 12

<!-- page 3 -->
TLP:CLEAR

1. INTRODUCTION Depuis une dizaine d’années, la communauté dédiée à la lutte contre la manipulation de l’information (LMI) se structure, et ses membres (entités gouvernementales, médias, ONG, etc.) multiplient les initiatives pour adopter des concepts communs permettant de mieux comprendre, analyser, et décrire la menace informationnelle numérique. Si certains de ces concepts sont déjà bien appréhendés et exploités par l’écosystème, comme ceux d’« opération informationnelle » ou de « tactiques, techniques & procédures » (TTP), les experts décrivent néanmoins de manière différente les ensembles d’éléments techniques 1 , comportementaux 2 et contextuels 3 qu’ils observent – alors même qu’ils disposent de capteurs et de méthodologies d’analyse similaires.

Cette absence de grammaire opérationnelle commune est susceptible de nuire à la bonne compréhension de la menace informationnelle. À titre d’exemple, les activités numériques malveillantes attribuées à l’entreprise russe « Social Design Agency »4 (SDA) sont généralement rassemblées sous le nom de Doppelgänger, mais ce dernier a tour à tour été présenté comme une « opération », une « campagne », un « réseau », ou même un « acteur malveillant » (« Threat Actor »)5. Dans certains cas, le nom le plus usité par la communauté pour décrire ces ensembles est, par ailleurs, le même que celui de l’acteur malveillant (comme Lakhta6), ou ne recouvre qu’une petite partie de l’ensemble des éléments liés à un acteur malveillant (comme Pravda7), augmentant encore la confusion.

Le besoin de nommer ces ensembles est pourtant bien compris par la communauté de LMI, qui a déjà popularisé des noms tels que Spamouflage8 ou Matriochka9. Certaines organisations proposent même désormais, à partir de concepts issus du domaine de la Cyber Threat Intelligence (CTI), des conventions de nommage intégrant les acteurs de la menace informationnelle numérique10. Afin de résoudre ces difficultés et éventuelles confusions, VIGINUM a défini et propose d’employer le concept de « mode opératoire informationnel » (MOI), et son équivalent en langue anglaise, « Information Manipulation Set » (IMS). Ce concept a pour principaux avantages :

- d'offrir une dénomination simple, adaptée et centrée sur les éléments techniques, comportementaux et contextuels identifiés et suivis par la communauté ;

- de fonctionner même si les attaquants ne sont pas connus, ni présager de leur origine, de leurs intentions ou de leur niveau de ressources (humaines, financières et techniques) ;

- d’être interopérable avec des concepts éprouvés issus de la CTI, ainsi que les standards et les

Adresses IP, enregistrements Whois, certificats SSL, empreintes de navigateur, etc. Partages, contenus identiques, dates de création d’un compte de réseau social, etc. Victimologie, motivations des activités malveillantes, opportunités, effets finaux recherchés, etc. Entreprise conduisant des opérations informationnelles numériques au profit de l’Administration présidentielle russe. Cf. https://mpf.se/psychological-defence-agency/publications/archive/2025-05-15-beyond-operation-doppelganger-a-capability- assessment-of-the-social-design-agency. VIGINUM s’appuie sur la définition d’acteur malveillant, ou acteur de la menace, proposée dans la documentation du modèle STIX : « Threat Actors are actual individuals, groups, or organizations believed to be operating with malicious intent ». Source : https://docs.oasis-open.org/cti/stix/v2.1/os/stix-v2.1-os.html#_k017w16zutw. Lakhta étant à la fois le nom d’un ensemble d’éléments techniques, comportementaux et contextuels, et le nom de l’organisation clandestine à l’origine de ces activités. Cf. https://www.sgdsn.gouv.fr/publications/guerre-en-ukraine-trois-annees- doperations-informationnelles-russes. Pravda étant le nom d’un réseau de sites liés à Portal Kombat, qui constitue un ensemble plus large et préexistant à ce réseau. Cf. https://www.sgdsn.gouv.fr/files/files/20240212_NP_SGDSN_VIGINUM_PORTAL-KOMBAT-NETWORK_ENG_VF.pdf. Cf. https://graphika.com/reports/spamouflage. Cf. https://www.sgdsn.gouv.fr/files/files/20240611_NP_SGDSN_VIGINUM_Matriochka_EN_VF.pdf. Comme Microsoft : https://learn.microsoft.com/en-us/unified-secops/microsoft-threat-actor-naming.

<!-- page 4 -->
TLP:CLEAR

outils déjà exploités au sein de la communauté de la LMI, comme le modèle STIX11 et OpenCTI.

Utilisé dans trois rapports techniques publiés par VIGINUM en 2025, le concept de MOI a depuis été réemployé dans les travaux d’autres acteurs de la communauté de LMI, à l’image du Service européen d’action extérieur (SEAE), de l’Agence de l'Union européenne pour la cybersécurité (ENISA) 12 et des membres du Doppelgänger Working Group (DGWG)13.

La présente note propose une définition précise de « mode opératoire informationnel », qui s’articule avec d’autres concepts utilisés par VIGINUM pour représenter la manipulation de l’information (cf. section 2.1). Elle vise également à aider la communauté à identifier des MOI parmi les ensembles suivis, en détaillant leurs principales caractéristiques (section 2.3) et en proposant des exemples concrets à partir d’activités malveillantes connues en source ouverte, attribuées ou non, issues de différents types d’acteurs, et observées dans différentes régions du monde (section 3).

Cf. https://oasis-open.github.io/cti-documentation/stix/intro.html. Cf. https://www.enisa.europa.eu/sites/default/files/2026-01/ENISA%20Threat%20Landscape%202025_v1.2.pdf. Cf. https://www.disinfo.eu/wp-content/uploads/2026/01/20260115_building-a-common-operational-picture-of-FIMI_01.pdf.

<!-- page 5 -->
TLP:CLEAR

2. DEFINITIONS 2.1 Les dimensions de la manipulation de l’information

Pour bien comprendre l’intérêt du concept de MOI, il convient de clarifier dans un premier temps la façon dont sont schématisées les activités informationnelles numériques malveillantes. Le concept s’articule en effet avec d’autres dénominations existantes, permettant ainsi de représenter de manière holistique les « échelles » de la manipulation de l’information : depuis les activités visibles en ligne (un compte de réseau social diffusant un contenu) jusqu’aux personnes physiques qui en sont à l’origine (opérateurs, voire commanditaires). Cette représentation s’appuie sur des concepts issus de la doctrine militaire, qui sépare traditionnellement la guerre en cinq « niveaux » ou « dimensions » : politique, stratégique, opérative, tactique et technique.

Transposés à la manipulation de l’information, ces cinq niveaux correspondent :

- politique : aux acteurs ou « commanditaires » qui décident d’exploiter le levier des activités informationnelles numériques parmi une gamme d’actions possibles, qu’il s’agisse d’un État ou de tout autre type d’acteur (entreprise privée, ONG, influenceur, etc.) ;

- stratégique : aux acteurs ou « opérateurs » missionnés, recrutés ou financés pour traduire les objectifs politiques en actions, quel que soit leur type (État, organisation, individu), les commanditaires ne possédant pas systématiquement ces moyens en propre ;

- opératif : aux campagnes informationnelles conduites par les opérateurs pour atteindre les objectifs stratégiques, traduits en objectifs informationnels (promouvoir, dénigrer, polariser, inciter à l’action dans le champ physique, etc.) ;

- tactique : aux opérations informationnelles conduites par les opérateurs pour remplir les objectifs opératifs et décliner en actions les campagnes, par exemple la publication de contenus polarisants, et qui exploitent une ou plusieurs infrastructures numériques ;

- technique : aux infrastructures numériques (« digital assets ») contrôlées et exploitées par les opérateurs pour remplir les objectifs tactiques, qu’il s’agisse de comptes de réseaux sociaux, de sites Internet, d’adresses courriels, etc.

Néanmoins, comme c’est également le cas pour les attaques informatiques, les opérateurs des activités informationnelles numériques malveillantes ne sont pas toujours connus, et les défenseurs doivent alors créer des ensembles fictifs (« Matriochka », « Spamouflage », « Doppelgänger ») regroupant les éléments techniques, comportementaux et contextuels identifiés qui sont estimés imputables à un seul acteur malveillant. Indispensable pour analyser les activités numériques malveillantes dans le brouillard des opérations informationnelles et suivre la menace sur le long terme, VIGINUM propose de définir ce niveau d’abstraction avec la notion de « mode opératoire informationnel ».

2.2 Définition d’un « mode opératoire informationnel »

VIGINUM définit un mode opératoire informationnel (MOI) comme un ensemble de comportements, d’outils et de tactiques, techniques et procédures (TTP) adverses présumés liés au même acteur malveillant ou groupe d’acteurs malveillants, qui peut être inconnu. Un ou plusieurs MOI peuvent être attribués techniquement à un acteur malveillant, et une ou plusieurs campagnes peuvent être imputées à un MOI. Un MOI ne doit pas être confondu avec un acteur malveillant (« Threat Actor »), qui peut être un État, une organisation ou un individu. Enfin, un MOI peut être exploité pour conduire des campagnes informationnelles, qui peuvent être constituées de plusieurs opérations informationnelles (ou incidents).

<!-- page 6 -->
TLP:CLEAR

Le concept de MOI permet ainsi de faire le lien entre les activités numériques observées et les opérateurs de ces activités, qu’ils soient identifiés ou non. Il se place donc au niveau stratégique en tant qu’émanation de l’acteur malveillant, et ensemble de moyens utilisés pour conduire les campagnes informationnelles. D’après cette définition, un même acteur malveillant peut conduire ou avoir opéré plusieurs modes opératoires informationnels.

Le concept de « mode opératoire informationnel » a été pensé comme le pendant dans le champ informationnel du concept de « mode opératoire d’attaque » (MOA) utilisé en CTI14, qui représente des ensembles à l’origine d’intrusions informatiques. Sa traduction, à savoir « Information Manipulation Set » (IMS), est également construite sur l’équivalent anglais de MOA, « Intrusion Set ». Ci-dessous figure un premier exemple intégrant le concept de mode opératoire informationnel, illustré par des activités numériques malveillantes imputées publiquement à Doppelgänger15 :

> **[Figure F1 - p. 6] Schéma de modèle - les cinq niveaux de la manipulation de l'information appliqués à Doppelgänger (sans légende dans le document)**
>
> Schéma vertical de référence de la doctrine : une chaîne de six blocs reliés par des traits verticaux (du haut vers le bas), groupés à gauche par accolades en cinq « niveaux » de la doctrine militaire transposée à la manipulation de l'information. Il illustre, avec les activités imputées publiquement à Doppelgänger, comment le concept de MOI s'articule avec les autres échelles.
>
> Blocs, de haut en bas, avec le niveau auquel chacun est rattaché :
> - Niveau politique : Commanditaires (Administration présidentielle russe).
> - Niveau stratégique (accolade unique englobant deux blocs) : Acteur malveillant (entreprise russe Social Design Agency) ; puis Mode opératoire informationnel (Doppelgänger).
> - Niveau opératif : Campagnes informationnelles (accuser l'Ukraine, l'« Occident » de crimes de guerre).
> - Niveau tactique : Opérations informationnelles (faux sites, cartes et listes de victimes).
> - Niveau technique : Infrastructures numériques (tribunalukraine.info, truemaps.info, etc.).
>
> Chaîne de relations lue de haut en bas (chaque trait relie un bloc au suivant) :
> - Commanditaires (Administration présidentielle russe) → [trait non étiqueté ; selon le texte, les opérateurs traduisent les objectifs des commanditaires] → Acteur malveillant (Social Design Agency)
> - Acteur malveillant (Social Design Agency) → [trait non étiqueté ; le MOI est l'émanation de l'acteur] → Mode opératoire informationnel (Doppelgänger)
> - Mode opératoire informationnel (Doppelgänger) → [trait non étiqueté ; le MOI sert à conduire les campagnes] → Campagnes informationnelles
> - Campagnes informationnelles → [trait non étiqueté ; les opérations déclinent les campagnes] → Opérations informationnelles
> - Opérations informationnelles → [trait non étiqueté ; les opérations exploitent les infrastructures] → Infrastructures numériques
> (le schéma ne porte que des traits non étiquetés ; les précisions entre crochets viennent du texte de la section 2.1).
>
> Définition des niveaux donnée par le texte : politique = commanditaires qui décident de recourir aux activités informationnelles numériques ; stratégique = opérateurs missionnés, recrutés ou financés pour traduire les objectifs politiques ; opératif = campagnes conduites pour atteindre les objectifs stratégiques traduits en objectifs informationnels (promouvoir, dénigrer, polariser, inciter à l'action physique) ; tactique = opérations qui déclinent les campagnes (par exemple la publication de contenus polarisants) ; technique = infrastructures numériques (« digital assets » : comptes de réseaux sociaux, sites, adresses courriels) contrôlées par les opérateurs.
>
> Ce que la figure enseigne : le MOI se place au niveau stratégique, comme émanation de l'acteur malveillant et ensemble de moyens servant à conduire les campagnes ; il est distinct de l'acteur (ici Social Design Agency est l'acteur malveillant, Doppelgänger est le MOI) et sert de niveau d'abstraction quand les opérateurs sont inconnus. La hiérarchie à retenir pour modéliser est Commanditaire → Acteur → MOI → Campagne → Opération → Infrastructure ; un acteur peut avoir plusieurs MOI, un MOI plusieurs campagnes, une campagne plusieurs opérations. Doppelgänger est ici l'exemple de MOI imputé publiquement (acteur : Social Design Agency ; commanditaire : Administration présidentielle russe).

2.3 Principales caractéristiques d’un MOI

Le concept de MOI répond avant tout à un besoin de clarification. Afin de faciliter l’identification des MOI parmi les ensembles suivis par la communauté de la LMI, VIGINUM propose de les confronter à deux conditions cumulatives qui constituent l’épine dorsale du concept : la clandestinité et la coordination. L’explication de ces deux conditions, illustrées par de courts exemples, sera suivie par des

Cf. https://www.cert.ssi.gouv.fr/uploads/CERTFR-2021-CTI-004.pdf. Cf. https://www.sgdsn.gouv.fr/files/files/Publications/20230719_NP_VIGINUM_RAPPORT-CAMPAGNE-RRN_EN.pdf et https://www.justice.gov/archives/opa/pr/justice-department-disrupts-covert-russian-government-sponsored-foreign-malign- influence.

<!-- page 7 -->
TLP:CLEAR

cas d’étude plus approfondis (cf. section 3) permettant de mieux appréhender l’intérêt et les subtilités du concept.

La première condition, dite de « clandestinité », insiste sur les intentions malveillantes des opérateurs : un ensemble peut être considéré comme un MOI si et seulement si les opérateurs engagent des efforts manifestes pour éviter l’imputation, c’est-à-dire le rattachement des infrastructures numériques entre elles 16 , et leur attribution à un acteur malveillant 17 . La clandestinité s’opère donc entre l’acteur malveillant et les infrastructures numériques sous leur contrôle, et permet d’exclure du concept tous les ensembles liés à des acteurs malveillants agissant en leur nom propre, ainsi que les ensembles non attribués disposant de différentes infrastructures numériques avec la même bannière.

➢ Exemple 1 : les opérateurs de Doppelgänger ont déployé des efforts évidents pour que le public

- et la communauté de la LMI – ne puisse pas lier techniquement leurs faux sites entre eux ni les imputer à l’entreprise russe Social Design Agency et leur commanditaire, à savoir l’Administration présidentielle russe. Doppelgänger peut donc être considéré comme un MOI dès lors qu’’il répond également à la seconde condition. ➢ Exemple 2 : les activités numériques d’une entité gouvernementale s’appuyant sur des canaux officiels, par exemple les comptes X de la diplomatie russe, ou les contenus liés à la « Fondation pour combattre l’injustice » (FCI)18, affichent tous clairement leur affiliation avec ces mêmes structures. Elles ne peuvent donc pas constituer des MOI à part entière. ➢ Exemple 3 : le média pro-Russe anglophone The Islander, qui n’est pas clairement imputé à un acteur, coordonne un ensemble d’infrastructures numériques (comptes X et Substack), et remplit à cet égard la seconde condition, mais ces infrastructures sont cependant clairement rattachables entre elles 19. Les activités numériques liées à The Islander ne peuvent donc pas constituer un MOI à part entière.

La seconde condition, dite de « coordination », insiste quant à elle sur le degré d’interaction entre les infrastructures numériques employées : un ensemble peut être considéré comme un MOI si et seulement si les infrastructures numériques sont employées de manière coordonnée, et présumées opérées par le même acteur ou groupe d’acteurs travaillant pour le même commanditaire. Cette condition permet d’assurer la cohérence des éléments techniques, qui doivent posséder des liens techniques ou comportementaux forts.

➢ Exemple 1 : les infrastructures numériques liées à Doppelgänger forment un ensemble homogène, car rattachables entre elles par un faisceau de liens techniques et comportementaux forts et convergents 20 . Doppelgänger peut donc être considéré comme un MOI s’il répond également à la première condition. ➢ Exemple 2 : des comptes TikTok publiant les mêmes contenus que d’autres comptes TikTok ou chaînes YouTube, ou des sites hébergés sur la même adresse IP, ne sont pas forcément opérés

Par exemple en créant des comptes de réseaux sociaux et des sites avec des noms, des chartes graphiques et des dates de création différentes, en évitant les interactions entre eux, en déposant et en hébergeant des sites via des services différents, etc. Par exemple en omettant d’afficher l’affiliation de sites ou de comptes de réseaux sociaux à l’acteur qui les contrôle, en indiquant de fausses informations de contact, en hébergeant des sites sur des serveurs partagés, dans un pays tiers, ou en les protégeant via des services d’anonymisation comme Cloudflare, etc. Fausse ONG créée par Evgueny PRIGOJINE en 2021 pour documenter les « violations des droits humains » dans les pays occidentaux. Cf. https://open.clemson.edu/cgi/viewcontent.cgi?article=1009&context=mfh_ci_reports. Elles disposent en effet du même nom, de la même charte graphique, redirigent directement les unes vers les autres, etc. Tels que la chaîne de diffusion, des éléments de code source, l’hébergement numérique, etc.

<!-- page 8 -->
TLP:CLEAR

par le même acteur malveillant. Ils ne peuvent donc pas constituer un MOI. ➢ Exemple 3 : les infrastructures numériques de Storm-167921 et de Matriochka constituent deux ensembles cohérents, parfois rassemblés sous le nom d’Overload, mais VIGINUM n’est pas en mesure de les lier techniquement entre eux, et continue donc pour l’heure de les considérer comme deux MOI distincts22. Les deux MOI pourraient être fusionnés si des éléments techniques soutiennent cette hypothèse dans le futur.

Les activités documentées par le Microsoft Threat Analysis Center (MTAC) sous cette appellation ne recouvrent que la publication de faux contenus par un groupe de chaînes Telegram présumées opérées, ou au moins coordonnées, par les mêmes acteurs malveillants. Cf. https://blogs.microsoft.com/on-the-issues/2024/06/02/russia-cyber-bots-disinformation-2024-paris- olympics/. Cf. https://checkfirst.network/operation-overload-an-ai-fuelled-escalation-of-the-kremlin-linked-propaganda-effort et https://www.sgdsn.gouv.fr/publications/guerre-en-ukraine-trois-annees-doperations-informationnelles-russes.

<!-- page 9 -->
TLP:CLEAR

3. EXEMPLES 3.1 Spamouflage

Largement documenté par la communauté de la LMI depuis 2019 23, Spamouflage désigne un ensemble d’éléments techniques, comportementaux et contextuels actif depuis au moins 2018, et également connu sous le nom de Dragonbridge, Storm-1376 et Taizi Flood. Les infrastructures numériques liées à Spamouflage serviraient principalement à diffuser des narratifs favorables aux intérêts du Parti communiste chinois (PCC) auprès d’une audience internationale et à discréditer des opposants au PCC, notamment en amont de périodes électorales, sur des plateformes telles que X, Facebook, YouTube, Reddit ou encore TikTok.

Exemples de publications liées publiquement au MOI Spamouflage. Source : Institute for Strategic Dialogue

> **[Figure F2 - p. 9] Captures de publications (X, Facebook) - exemples du MOI Spamouflage, source Institute for Strategic Dialogue**
>
> Trois captures juxtaposées, légendées « Exemples de publications liées publiquement au MOI Spamouflage. Source : Institute for Strategic Dialogue » :
> 1. Publication X (capture « Twitter Web App », horodatée 3:46 AM - Jun 2, 2020), compte au nom en caractères chinois dont le handle semble être @ciacho20 [partiellement illisible]. Texte en anglais : « The United States intervened in Hong Kong affairs, but the "beautiful scenery line" that the Secretary of State said was staged in the United States. The double standard of American democracy has remained unchanged. HongKongPolice » suivi de mots-dièses/termes en chinois (香港警察, 香港 国安法). Image jointe : visuel sur fond bleu avec un long texte en chinois (illisible à cette résolution) et trois photos (un incendie avec un drapeau américain, la Statue de la Liberté, des policiers maîtrisant une personne au sol).
> 2. Publication Facebook de la page « Lilly Anderson » (il y a 3 j), vidéo de 2:27 avec incrustation en anglais et en chinois : « As the Portland protests continue to escalate, Trump will end up in a bind of his own. » et sa traduction en chinois (波特兰抗议示威持续升级，特朗普终将作茧自缚) ; image de manifestants et de policiers en tenue anti-émeute. Elle est reposée par une page dont le nom est en chinois (沙雕俱乐部 [lecture probable]), avec le même texte en anglais et en chinois en légende ; 22 vues affichées, bouton « Like Page ».
> 3. Publication X (handle illisible) montrant un visuel généré ou retouché de style dramatique : les deux dirigeants américains (Donald Trump et Joe Biden, d'après l'illustration) s'affrontent à l'épée devant un drapeau américain déchiré et enflammé, avec le texte « POLITICAL CONFLICT / CHAOS IN AMERICAN POLITICS » ; horodatage 11:40 AM · Jan 17, 2024, environ 4 300 vues [chiffre peu lisible].
>
> Les trois captures sont en anglais (avec du chinois) et visent une audience internationale ; le rapport ne signale pas de flèche ni d'encadré d'inauthenticité sur ces images.
>
> Ce que la figure enseigne : ce sont les exemples du contenu type de Spamouflage (alias Dragonbridge, Storm-1376, Taizi Flood, actif depuis au moins 2018) : narratifs favorables aux intérêts du Parti communiste chinois et dénigrement des États-Unis, des opposants au PCC et du jeu politique américain, diffusés sur X, Facebook, YouTube, Reddit et TikTok, y compris en amont d'échéances électorales ; publications bilingues anglais/chinois, contenus visuels bruts, faible engagement affiché (22 vues pour l'une). Illustration d'un MOI non formellement imputé, constitué de comptes qui masquent leurs liens et sont utilisés de manière coordonnée.

Sur la base des éléments publiés, VIGINUM peut décrire Spamouflage comme un mode opératoire informationnel à part entière, encore non formellement imputé à un acteur malveillant, car il est constitué :

- de comptes de réseaux sociaux masquant ostensiblement leurs liens entre eux et avec leurs opérateurs, dans le but probable d’éviter leur modération par les plateformes et leur rattachement à une entité gouvernementale chinoise ;

- de comptes de réseaux sociaux utilisés de manière coordonnée, rattachés entre eux par des liens techniques et comportementaux forts (date de création, pseudonymes, éléments biographiques, nombre d’abonnés, etc.), et présumés opérés par le même acteur malveillant.

3.2 Activités clandestines liées au Baku Initiative Group

Documenté par VIGINUM 24 , le Baku Initiative Group (BIG) est une ONG fondée en juillet 2023 en Azerbaïdjan. L’organisation diffuse depuis sa création des contenus hostiles à la France sous l’angle de la « lutte contre le néocolonialisme », en exploitant notamment la situation économique et politique dans les territoires ultramarins, ainsi que les mouvements et idées indépendantistes. La majeure partie des activités numériques du BIG (sites Internet et comptes de réseaux sociaux) sont conduites au nom

Cf. https://graphika.com/reports/spamouflage, https://cloud.google.com/blog/topics/threat-intelligence/prc-dragonbridge- influence-elections, https://www.isdglobal.org/digital_dispatches/pro-ccp-spamouflage-campaign-experiments-with-new-tactics- targeting-the-us/ et https://edition.cnn.com/2023/11/13/us/china-online-disinformation-invs/index.html. Cf. https://www.sgdsn.gouv.fr/files/files/Publications/20241202_NP_SGDSN_VIGINUM_RAPPORT-BIG.pdf.

<!-- page 10 -->
TLP:CLEAR

de l’organisation, qui est donc considérée comme un acteur malveillant. VIGINUM estime toutefois qu’en parallèle de ses activités publiques, le BIG opère un mode opératoire informationnel, puisqu’à plusieurs reprises, il s’est appuyé pour amplifier ses narratifs sur un ensemble de comptes :

- masquant ostensiblement, malgré un succès limité, leurs liens entre eux et avec le pouvoir azerbaïdjanais, dans le but évident de maintenir un déni plausible et de donner l’impression d’une contestation organique ;

- opérés de manière coordonnée, possédant des liens techniques et comportementaux forts (hashtags, messages et période d’activité, etc.), et très probablement opérés par le même groupe d’acteurs malveillants25.

Exemples de publications de comptes clandestins amplifiant les narratifs du Baku Initiative Group. Source : VIGINUM

> **[Figure F3 - p. 10] Captures de publications X - comptes clandestins amplifiant les narratifs du Baku Initiative Group, source VIGINUM**
>
> Deux captures de publications X, légendées « Exemples de publications de comptes clandestins amplifiant les narratifs du Baku Initiative Group. Source : VIGINUM » :
> 1. Compte « Ayshan Aslan-Mammadli » (badge de vérification bleu, handle @AyshanASLAN), publication du 12:42 PM · May 15, 2024, 410 vues, 1 réponse, 1 repartage, 17 mentions « j'aime ». Texte (anglais) : « The [drapeau de la France] French police are murderers - the Algerian murders continue. #RecognizeNewCaledonia #EndFrenchColonialism #FrenchColonialism #BoycottParis2024 #Paris2024 ». Média : montage vidéo de 0:21 en quatre cases avec les légendes « LA POLICE DE FRANCE » et « THE FRENCH POLICE » : scènes de violences policières (une personne au sol couverte de sang, des policiers entourant une personne à terre) rapprochées d'un départ de course d'athlétisme.
> 2. Compte « Antoine Faubarande » (handle @Antoine1524, publié « 5h »), texte (anglais) : « The french police are murderers - Algerian Murders continues #RecognizeNewCaledonia #EndFrenchColonialism #FrenchColonialism #BoycottParis2024 #Paris2024 ». Média : vidéo de 0:36 (lecture à 0:02) montrant un panorama de Paris au coucher du soleil avec le logo « PARIS 2024 » et les anneaux olympiques ; 5 vues affichées.
>
> Indicateurs de coordination visibles : les deux comptes publient la même phrase d'accroche (« The french police are murderers - [the] Algerian murders continue ») avec exactement le même bloc de cinq hashtags (#RecognizeNewCaledonia #EndFrenchColonialism #FrenchColonialism #BoycottParis2024 #Paris2024) ; le second compte est quasiment sans engagement (5 vues). Le texte de la page cite les hashtags, les messages et la période d'activité comme liens techniques et comportementaux forts.
>
> Ce que la figure enseigne : exemple concret de la double condition du MOI appliquée au Baku Initiative Group (BIG, ONG fondée en juillet 2023 en Azerbaïdjan) : des comptes qui masquent leurs liens entre eux et avec le pouvoir azerbaïdjanais (clandestinité, déni plausible, apparence de contestation organique) tout en répétant des messages et hashtags identiques sur la même période (coordination). Les narratifs exploités : anticolonialisme visant la France, Nouvelle-Calédonie, appel au boycott de Paris 2024. Une ONG agissant en son nom propre est un acteur malveillant ; seul l'ensemble de comptes clandestins constitue le MOI.

Depuis au moins 2022, VIGINUM a noté à plusieurs reprises l’apparition de MOI liés à des acteurs malveillants préexistants, le premier permettant d’appuyer de manière clandestine les activités d’acteurs souhaitant renforcer le déni plausible, atteindre de nouvelles audiences, ou contourner des sanctions et restrictions techniques. Des médias russes liés ou proches de l’État, tels que RT26 ou Rybar27, auraient notamment commencé à conduire des MOI dans cette optique.

3.3 Storm-1516, CopyCop et Lakhta

Storm-1516, aussi connu publiquement sous le nom de Neva Flood, est un ensemble d’éléments techniques, comportementaux et contextuels actif depuis a minima août 2023. Cet ensemble est responsable de plus de 150 opérations informationnelles ciblant principalement les intérêts de l’Ukraine, de pays occidentaux et de pays post-soviétiques, notamment durant des processus électoraux. Storm-1516 se caractérise par une chaîne de diffusion complexe exploitant à la fois des infrastructures numériques en propre, mais également celle de relais financés ou liées à d’autres ensembles. À ce jour, VIGINUM estime que les opérations informationnelles imputées à Storm-1516 ont impliqué au moins trois ensembles clandestins et coordonnés, qui constituent chacun un mode

Cf. https://www.sgdsn.gouv.fr/publications/nouvelle-caledonie-manoeuvres-informationnelles-impliquant-des-acteurs-0 et https://www.sgdsn.gouv.fr/files/files/Publications/20241202_NP_SGDSN_VIGINUM_RAPPORT-BIG.pdf. Cf. https://www.justice.gov/archives/opa/pr/justice-department-leads-efforts-among-federal-international-and-private-sector- partners. Cf. https://dfrlab.org/2025/09/23/sanctioned-russian-actor-linked-to-new-media-outlet-targeting-moldova/.

<!-- page 11 -->
TLP:CLEAR

opératoire informationnel à part entière :

- Storm-1516, imputé par le gouvernement américain au renseignement militaire russe et à un think tank moscovite, le Centre d’expertise géopolitique (CGE), responsable de la coordination des opérations, de la création des contenus, des narratifs, et du dépôt et du maintien d’un nombre limité d’infrastructures numériques (chaînes YouTube et comptes X) ;

- CopyCop, imputé à un ancien policier américain exilé en Russie depuis 2016, John Mark DOUGAN, qui serait responsable de l’enregistrement et du maintien de faux sites Internet exploités par Storm-1516, mais également par d’autres acteurs malveillants russes et pro-Russes ;

- Lakhta, non formellement imputé mais lié historiquement à la galaxie PRIGOJINE, qui a participé aux opérations de Storm-1516 en primo-diffusant des contenus du MOI via ses infrastructures numériques (comptes X), et s’est appuyé sur celles de Storm-1516 pour ses propres opérations28.

Le concept de MOI se révèle donc essentiel pour analyser l’imbrication de plusieurs modes opératoires, puisqu’il permet de délimiter strictement des ensembles non liés par des éléments techniques, comportementaux ou contextuels, de mieux appréhender les aires de responsabilité et la structure des différents acteurs malveillants qui en sont responsables, et de suivre plus finement l’évolution de chacun des MOI impliqués dans le temps.

Schéma de diffusion des contenus de Storm-1516, s’appuyant notamment sur des éléments d’infrastructures numériques liés aux MOI CopyCop et Lakhta. Source : VIGINUM

> **[Figure F4 - p. 11] Schéma de processus en quatre phases - chaîne de diffusion de Storm-1516 avec les infrastructures des MOI CopyCop et Lakhta, source VIGINUM**
>
> Schéma intitulé « Schéma de diffusion des contenus de Storm-1516, s'appuyant notamment sur des éléments d'infrastructures numériques liés aux MOI CopyCop et Lakhta. Source : VIGINUM ». Un grand cadre rouge plein, libellé STORM-1516, contient quatre phases enchaînées de haut en bas ; deux zones en pointillés, libellées « CopyCop » (à gauche) et « Galaxie Prigojine » (à droite), chevauchent le cadre rouge et délimitent les éléments d'infrastructure qui relèvent de ces autres MOI.
>
> Blocs et sous-éléments (chaque phase éclate en éléments par des flèches) :
> - PHASE 1 : PLANIFICATION → conception des narratifs ; recrutement d'acteurs ; génération de deepfakes ; montage photo, audio et vidéo. (Phase entièrement dans le cadre rouge Storm-1516, hors des zones pointillées.)
> - PHASE 2 : PRIMO-DIFFUSION → sites dédiés ; comptes de réseaux sociaux ; comptes jetables ; tiers rémunérés ; comptes X liés à Lakhta. D'après le tracé des pointillés, « sites dédiés » et « comptes de réseaux sociaux » sont dans la zone CopyCop ; « comptes jetables » et « tiers rémunérés » sont au centre, dans le périmètre Storm-1516 seul ; « comptes X liés à Lakhta » est dans la zone Galaxie Prigojine.
> - PHASE 3 : BLANCHIMENT → médias étrangers rémunérés ; comptes de réseaux sociaux rémunérés. (Dans le cadre rouge, au centre, hors des deux zones pointillées.)
> - PHASE 4 : AMPLIFICATION → faux sites locaux ; comptes rémunérés ; espaces commentaires ; chaînes Telegram ; influenceurs BJA & FCI ; comptes X liés à Lakhta. D'après le tracé : « faux sites locaux » dans la zone CopyCop ; « comptes rémunérés », « espaces commentaires » et « chaînes Telegram » au centre ; « influenceurs BJA & FCI » et « comptes X liés à Lakhta » dans la zone Galaxie Prigojine.
>
> Relations d'ordre : Phase 1 → Phase 2 → Phase 3 → Phase 4 (flux descendant). L'attribution des éléments aux zones CopyCop / Galaxie Prigojine est lue sur le tracé des pointillés et reste approximative sur quelques éléments.
>
> Ce que la figure enseigne : la chaîne de diffusion de Storm-1516 en quatre temps (planification des narratifs et des contenus synthétiques, primo-diffusion par sites et comptes jetables, blanchiment par des médias étrangers et comptes rémunérés, amplification par faux sites locaux, comptes rémunérés, Telegram, commentaires et influenceurs). Storm-1516 conçoit les narratifs et les contenus ; la primo-diffusion et l'amplification s'appuient sur l'infrastructure d'autres MOI : CopyCop (faux sites) et Lakhta, de la galaxie Prigojine (comptes X, primo-diffusion de contenus). Le texte de la page en tire la règle : le concept de MOI sert à délimiter des ensembles imbriqués, à distinguer les aires de responsabilité des acteurs et à suivre l'évolution de chacun dans le temps. Pour mémoire, selon le texte, Storm-1516 (alias Neva Flood) est actif depuis au moins août 2023 et responsable de plus de 150 opérations informationnelles ; CopyCop est imputé à John Mark DOUGAN ; Storm-1516 est imputé par le gouvernement américain au renseignement militaire russe et à un think tank moscovite, le Centre d'expertise géopolitique (CGE) ; Lakhta n'est pas formellement imputé.

Cf. https://www.sgdsn.gouv.fr/publications/analyse-du-mode-operatoire-informationnel-russe-storm-1516.

<!-- page 12 -->
TLP:CLEAR

3.4 « Team Jorge »

« Team Jorge » est le nom donné par le consortium de journalistes Story Killers 29 à une officine israélienne ayant conduit des campagnes de manipulation de l’information au profit de divers clients publics et privés depuis au moins 2015, notamment durant des processus électoraux. Pour ce faire, Team Jorge s’appuyait notamment sur un outil, appelé Advanced Impact Media Solutions (AIMS), permettant de gérer la création de faux articles de presse et de dizaines de milliers de profils inauthentiques sur les réseaux sociaux, dont Facebook, Twitter, Instagram, YouTube ou encore Reddit.

Les campagnes attribuées à Team Jorge ont notamment servi à promouvoir des projets pour divers clients privés, mais également à dénoncer des initiatives et des personnalités, comme les sanctions contre des oligarques russes, la coupe du monde de football 2022 au Qatar, ou des hommes d’affaires asiatiques. Certaines opérations informationnelles numériques étaient par ailleurs combinées avec des intrusions informatiques.

Captures d’écran de l’outil AIMS employé par l’officine. Source : Forbidden Stories

> **[Figure F5 - p. 12] Captures d'écran d'une interface logicielle - outil AIMS (Advanced Impact Media Solutions) de « Team Jorge », source Forbidden Stories**
>
> Deux captures empilées de l'interface web de l'outil AIMS (« Advanced Impact Media Solutions »), thème sombre, légendées « Captures d'écran de l'outil AIMS employé par l'officine. Source : Forbidden Stories » (résolution faible, la plupart des libellés sont illisibles) :
> 1. Tableau de bord : logo AIMS en haut à gauche, menu latéral gauche d'une quinzaine d'entrées [libellés illisibles, dont probablement « Dashboard »], titre « Advanced Impact Media Solutions » avec un sous-titre [illisible], quatre grandes tuiles de compteurs colorées (rose, orange, bleu, violet) avec mini-courbes, quatre tuiles supplémentaires de compteurs à zéro ou presque [valeurs illisibles], une carte du monde en teinte bleue avec une légende [illisible], et à droite le logo AIMS avec un bloc « Technical support » [contenu illisible].
> 2. Liste de profils : galerie de fiches de profils de réseaux sociaux inauthentiques en grille 4 colonnes, chaque fiche avec photo de profil, nom, note en étoiles (4 à 5 étoiles) et icônes des réseaux sociaux associés. Noms visibles (lecture partielle) : Susan Johnson, Clara Meyer, Hannah Wagner, Jordan Thompson, Daphne Wilson [incertain], Fatima Nasser, Abdul Hadi [incertain], Jordan Cook. Les photos sont de portraits variés (femmes, un homme, une photo de paysage).
>
> Ce que la figure enseigne : AIMS est l'outil de gestion de la création de faux articles de presse et de dizaines de milliers de profils inauthentiques sur Facebook, Twitter, Instagram, YouTube et Reddit, utilisé par l'officine israélienne « Team Jorge » depuis au moins 2015 pour des clients publics et privés. L'interface montre une industrialisation de la clandestinité : profils prêts à l'emploi, notés, gérés en bloc, avec statistiques par pays. Le texte en tire que « Team Jorge » conduit un MOI : liens entre infrastructures et officine masqués (anonymat des clients, évitement de poursuites), infrastructures coordonnées (création, suivi, abonnements) et directement opérées par l'officine. Le concept s'applique donc aussi aux officines et aux structures privées agissant au profit de clients.

VIGINUM estime que l’acteur malveillant « Team Jorge » a conduit un mode opératoire informationnel au profit de ses clients, car :

- l’acteur malveillant a manifestement cherché à cacher les liens entre ses différentes infrastructures numériques et l’officine, dans le but probable de garantir l’anonymat de ses clients et d’éviter des poursuites légales ;

- les infrastructures numériques étaient utilisées de manière coordonnée, puisqu’elles participaient à des campagnes précises, présentaient des liens techniques et comportementaux

Cf. https://forbiddenstories.org/fr/story-killers.

<!-- page 13 -->
TLP:CLEAR

forts (date de création, suivi, abonnements, etc.), et étaient directement opérées par l’officine.

Le concept de MOI est donc applicable à tout type d’acteurs (entités gouvernementales, structures privées, officines agissant au travers de sociétés-écrans), même s’il implique un groupe d’acteurs malveillants opérant différentes parties du MOI (définition des narratifs, dépôt d’infrastructures numériques, création des contenus, etc.), même s’il est mis à disposition de divers « clients » ou commanditaires, et quelles que soient leurs intentions.

<!-- page 14 -->
TLP:CLEAR

À PROPOS DE VIGINUM

Créé le 13 juillet 2021 et rattaché au SGDSN, le service de vigilance et de protection contre les ingérences numériques étrangères (VIGINUM) a pour raison d’être la protection du débat public numérique touchant aux intérêts fondamentaux de la Nation.

Ce service technique et opérationnel de l’État a pour mission de détecter et caractériser les campagnes de manipulation de l’information sur les plateformes numériques, impliquant des acteurs étrangers dans le but de nuire à la France et à ses intérêts.

Service de vigilance et protection contre les ingérences numériques étrangères | SGDSN

Crédit photo couverture : Photo de Piermanuele Sberni sur Unsplash.
