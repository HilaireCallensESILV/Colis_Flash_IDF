"""Classes métier : Colis, Client, Camion, Conducteur, et paramètres de la simulation."""
from __future__ import annotations

from dataclasses import dataclass, field

VOLUME = {"petit": 1, "moyen": 3, "grand": 5}          # volume d'un colis selon sa taille
JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


@dataclass
class Params:
    """Hypothèses de la simulation (modifiables)."""
    seed: int = 42
    entreprise: str = "ColisFlash IDF"
    entrepot_nom: str = "ColisFlash IDF — Entrepôt de Gennevilliers"
    entrepot_lat: float = 48.9440
    entrepot_lon: float = 2.2860
    vitesse_kmh: float = 40.0
    facteur_detour: float = 1.3          # distance route = distance à vol d'oiseau x 1.3
    service_min_par_colis: int = 2       # 1er colis d'un client : 2 minutes (dépôt rapide)
    service_min_colis_suppl: int = 1     # puis 1 minute par colis supplémentaire du même client
    debut_min: int = 8 * 60              # départ de l'entrepôt : 8h00
    fin_min: int = 16 * 60               # retour au plus tard : 16h00
    pause_min: int = 60                  # pause déjeuner dans le camion (à partir de 4h d'activité)
    p_prioritaire: float = 0.20          # client prioritaire : 1 chance sur 5
    p_deuxieme_colis: float = 0.20       # 2e colis : 1 chance sur 5
    p_troisieme_colis: float = 0.10      # 3e colis : 1 chance sur 10 (si 2e colis)
    p_panne: float = 0.005               # panne d'un camion chaque matin : 0,5 % (entretien préventif)
    capacite_grand: int = 200
    capacite_petit: int = 100
    n_grands_camions: int = 2
    n_petits_camions: int = 3
    n_conducteurs: int = 4
    conges_min_mois: int = 3             # congés payés minimum par mois
    jours_par_mois: int = 28             # 1 mois = 4 semaines
    temps_solveur_s: int = 10            # temps de calcul OR-Tools par journée
    max_candidats: int = 200             # clients examinés par le solveur chaque jour
    solveur: str = "auto"                # "auto" : OR-Tools si disponible, sinon heuristique de secours ; ou "ortools" / "heuristique"

    # --- rotations
    rechargement_min: int = 20           # temps de rechargement à l'entrepôt entre deux tournées
    max_rotations: int = 3               # nombre maximum de tournées par camion et par jour

    # --- trajets (OSRM)
    use_osrm: bool = True                # False = estimation vol d'oiseau x 1.3 (hors ligne)
    osrm_url: str = "https://router.project-osrm.org"
    osrm_bloc: int = 50                  # taille des blocs de la matrice (le serveur public limite le nombre de points)
    osrm_timeout: int = 20
    coef_circulation: float = 1.2        # OSRM ne connaît pas le trafic : durées multipliées par ce coefficient

    # --- coûts (euros)
    smic_horaire: float = 12.31          # SMIC horaire brut au 1er juin 2026
    heures_semaine: float = 35.0         # durée légale, pas d'heures supplémentaires
    taux_charges_patronales: float = 0.0 # 0 = salaire brut seul (modifiable)
    cout_km: float = 0.10                # carburant par kilomètre
    cout_stockage_colis_jour: float = 0.02   # par colis restant en fin de journée
    cout_retard_colis_jour: float = 0.05     # par colis restant en fin de journée (jour de retard)
    cout_panne: float = 500.0            # réparation, par panne
    marge_petit: float = 4.0             # marge par colis livré selon sa taille (avant frais de livraison) : prix x 2
    marge_moyen: float = 7.0
    marge_grand: float = 12.0
    supplement_express: float = 2.0      # supplément par colis d'un client prioritaire livré
    cout_entretien_camion_jour: float = 3.0   # entretien préventif, par camion possédé et par jour
    cout_entrepot_jour: float = 100.0    # total par jour : employé d'entrepôt + location + électricité, etc.
    cout_camion_jour: float = 5.0        # grand camion possédé, par jour
    cout_camion_petit_jour: float = 3.0  # petit camion possédé, par jour (moins cher)

    # --- supplément zone éloignée
    zone_eloignee_km: float = 20.0       # au-delà de cette distance (à vol d'oiseau) de l'entrepôt
    supplement_zone_eloignee: float = 1.0    # par colis livré dans cette zone

    # --- livraison par secteur : seules 2 des 4 zones (Nord, Est, Sud, Ouest) sont livrées chaque jour
    livraison_par_secteur: bool = True
    secteurs_par_jour: int = 2           # les clients prioritaires sont livrés quel que soit leur secteur

    def marge_colis(self, taille: str) -> float:
        return {"petit": self.marge_petit, "moyen": self.marge_moyen, "grand": self.marge_grand}[taille]

    def cout_camion(self, taille: str) -> float:
        return self.cout_camion_jour if taille == "grand" else self.cout_camion_petit_jour

    def duree_service_min(self, n_colis: int) -> int:
        """Durée de l'arrêt chez un client : 2 min pour le 1er colis + 1 min par colis supplémentaire."""
        return self.service_min_par_colis + max(n_colis - 1, 0) * self.service_min_colis_suppl

    @property
    def cout_conducteur_jour(self) -> float:
        """Salaire payé chaque jour (repos et congés compris) : 35 h x SMIC / 7 jours."""
        return self.smic_horaire * self.heures_semaine / 7 * (1 + self.taux_charges_patronales)

    @property
    def cout_employe_entrepot_jour(self) -> float:
        """Employé d'entrepôt : même système qu'un conducteur (35 h par semaine, payé 7 jours sur 7)."""
        return self.cout_conducteur_jour

    @property
    def cout_location_entrepot_jour(self) -> float:
        """Reste du coût de l'entrepôt : location, électricité, charges."""
        return max(self.cout_entrepot_jour - self.cout_employe_entrepot_jour, 0.0)

    @property
    def minutes_actives_max(self) -> int:
        """Temps actif max (conduite + livraisons) : 8h de journée moins 1h de pause = 7h."""
        return (self.fin_min - self.debut_min) - self.pause_min


@dataclass
class Colis:
    id: str
    client_id: str
    date_commande: int                   # numéro du jour de simulation (0 = premier lundi)
    taille: str                          # "petit" | "moyen" | "grand"
    lat: float
    lon: float
    livre: bool = False

    @property
    def volume(self) -> int:
        return VOLUME[self.taille]


@dataclass
class Client:
    id: str
    nom: str
    prenom: str
    prioritaire: bool
    lat: float
    lon: float
    adresse: str
    colis: list = field(default_factory=list)
    secteur: str = ""                    # Nord | Est | Sud | Ouest (par rapport à l'entrepôt)
    distance_km: float = 0.0             # distance à vol d'oiseau de l'entrepôt

    @property
    def colis_restants(self) -> list:
        return [c for c in self.colis if not c.livre]

    @property
    def volume_restant(self) -> int:
        return sum(c.volume for c in self.colis_restants)

    @property
    def date_commande(self) -> int:
        return min(c.date_commande for c in self.colis)


@dataclass
class Camion:
    id: str
    plaque: str
    taille: str                          # "grand" | "petit"
    capacite: int
    en_panne: bool = False
    conducteur_id: str | None = None     # conducteur rattaché pour la journée


@dataclass
class Conducteur:
    id: str
    nom: str
    prenom: str
    jours_repos: tuple                   # 2 jours de repos fixes par semaine (0 = lundi)
    echeance_conges: int = 27            # jour du mois avant lequel les 3 congés doivent être pris
    conges_payes_pris: int = 0           # dans le mois en cours
    conges_offerts: int = 0              # congés supplémentaires offerts dans le mois en cours
    historique: dict = field(default_factory=dict)   # {numéro de jour: statut}

    def est_repos(self, jour_semaine: int) -> bool:
        return jour_semaine in self.jours_repos
