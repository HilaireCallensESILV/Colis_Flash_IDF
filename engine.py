"""
Moteur de la simulation :
    AddressPool   -> adresses GPS en Île-de-France (réelles si le pool BAN existe, sinon aléatoires)
    OsrmClient    -> appels au serveur OSRM (matrices de durées/distances, tracé des routes)
    TravelModel   -> matrices de trajet de la journée (OSRM avec cache, sinon estimation)
    DailyPlanner  -> choix des clients, camions, rotations et tournées de la journée (OR-Tools)
    Simulation    -> état global, conducteurs, congés, pannes, coûts, journées successives
"""
from __future__ import annotations

import io
import random
import string
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from models import (JOURS, Camion, Client, Colis, Conducteur, Params)

PRENOMS = ["Lucas", "Emma", "Hugo", "Léa", "Louis", "Chloé", "Nathan", "Manon", "Jules", "Camille",
           "Adam", "Inès", "Gabriel", "Sarah", "Théo", "Zoé", "Rayan", "Lina", "Noah", "Jade",
           "Maxime", "Yasmine", "Antoine", "Clara", "Karim", "Nora", "Mehdi", "Eva", "Paul", "Anaïs"]
NOMS = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau",
        "Simon", "Laurent", "Lefebvre", "Michel", "Garcia", "David", "Bertrand", "Roux", "Vincent", "Fournier",
        "Benali", "Diallo", "Nguyen", "Silva", "Costa", "Haddad", "Traoré", "Lopez", "Girard", "Morel"]


_ORTOOLS = {"ok": None, "erreur": ""}


def ortools_disponible() -> bool:
    """Essaie d'importer OR-Tools une seule fois. Certaines politiques de sécurité Windows bloquent ses DLL."""
    if _ORTOOLS["ok"] is None:
        try:
            from ortools.constraint_solver import pywrapcp, routing_enums_pb2  # noqa: F401
            _ORTOOLS["ok"] = True
        except Exception as e:                      # ImportError, OSError (WinError 4551), etc.
            _ORTOOLS["ok"], _ORTOOLS["erreur"] = False, str(e)
    return _ORTOOLS["ok"]


SECTEURS = ["Nord", "Est", "Sud", "Ouest"]


def secteur_de(lat: float, lon: float, lat0: float, lon0: float) -> str:
    """Secteur d'un point par rapport à l'entrepôt : Nord, Est, Sud ou Ouest (quarts de 90°)."""
    angle = (np.degrees(np.arctan2((lon - lon0) * np.cos(np.radians(lat0)), lat - lat0)) + 360) % 360   # 0° = nord
    return SECTEURS[int(((angle + 45) % 360) // 90)]


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def hhmm(minutes: float) -> str:
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


# ----------------------------------------------------------------------------
# Adresses
# ----------------------------------------------------------------------------
class AddressPool:
    """Pool d'adresses GPS. Utilise data/address_pool.csv (créé par build_address_pool.py) s'il existe."""
    POP = {"75": 2.10, "77": 1.43, "78": 1.46, "91": 1.32, "92": 1.65, "93": 1.68, "94": 1.41, "95": 1.25}
    BAN_URL = "https://adresse.data.gouv.fr/data/ban/adresses/latest/csv/adresses-{dept}.csv.gz"

    def __init__(self, params: Params, data_dir: Path):
        self.p = params
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "address_pool.csv"
        self.df = pd.read_csv(self.path) if self.path.exists() else None
        self.reelles = self.df is not None

    def sample(self, n: int, rng: np.random.Generator) -> list:
        """Retourne n tuples (lat, lon, adresse)."""
        if self.reelles:
            w = self.df["weight"] / self.df["weight"].sum()
            rows = self.df.iloc[rng.choice(len(self.df), size=n, replace=True, p=w)]
            return list(zip(rows["lat"], rows["lon"], rows["adresse"]))
        # secours : points GPS aléatoires autour de l'entrepôt (peuvent tomber dans la Seine)
        out = []
        while len(out) < n:
            lat = self.p.entrepot_lat + rng.normal(0, 0.10)
            lon = self.p.entrepot_lon + rng.normal(0, 0.15)
            if haversine_km(self.p.entrepot_lat, self.p.entrepot_lon, lat, lon) <= 35:
                out.append((round(lat, 6), round(lon, 6), f"Point GPS aléatoire ({lat:.4f}, {lon:.4f})"))
        return out

    @classmethod
    def build_from_ban(cls, params: Params, data_dir: Path, par_dept: int = 4000, max_km: float = 35.0):
        """Construit data/address_pool.csv à partir de la Base Adresse Nationale (téléchargement une fois)."""
        import requests
        data_dir = Path(data_dir)
        raw = data_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        parts = []
        for dept, pop in cls.POP.items():
            path = raw / f"adresses-{dept}.csv.gz"
            if not path.exists():
                print(f"Téléchargement BAN département {dept}...")
                with requests.get(cls.BAN_URL.format(dept=dept), stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 20):
                            f.write(chunk)
            total, kept = 0, []
            for ch in pd.read_csv(path, sep=";", compression="gzip", chunksize=200_000,
                                  usecols=["numero", "nom_voie", "code_postal", "nom_commune", "lon", "lat"],
                                  dtype={"numero": str, "code_postal": str}):
                ch = ch[pd.to_numeric(ch["numero"], errors="coerce") > 0].dropna(subset=["lon", "lat"])
                total += len(ch)
                d = haversine_km(params.entrepot_lat, params.entrepot_lon, ch["lat"].values, ch["lon"].values)
                kept.append(ch[d <= max_km])
            df = pd.concat(kept)
            if df.empty:
                continue
            habitants_rayon = pop * 1e6 * len(df) / max(total, 1)    # habitants du département dans le rayon
            df = df.sample(min(par_dept, len(df)), random_state=params.seed)
            df["adresse"] = df["numero"] + " " + df["nom_voie"] + ", " + df["code_postal"] + " " + df["nom_commune"]
            df["weight"] = habitants_rayon / len(df)
            parts.append(df[["lat", "lon", "adresse", "weight"]])
            print(f"  dép. {dept}: {len(df)} adresses retenues")
        pd.concat(parts).to_csv(data_dir / "address_pool.csv", index=False)
        print("Pool d'adresses créé :", data_dir / "address_pool.csv")


# ----------------------------------------------------------------------------
# OSRM et matrices de trajet
# ----------------------------------------------------------------------------
class OsrmTooBig(Exception):
    pass


class OsrmClient:
    """Appels au serveur OSRM (profil voiture). Aucune donnée de trafic : durées « sans circulation »."""

    def __init__(self, params: Params):
        import requests
        self.p = params
        self.base = params.osrm_url.rstrip("/")
        self.block = params.osrm_bloc
        self.http = requests.Session()

    def _get(self, url: str) -> dict:
        derniere = None
        for essai in range(3):
            try:
                r = self.http.get(url, timeout=self.p.osrm_timeout)
                if r.status_code == 429:                    # trop de requêtes : on patiente
                    time.sleep(2 * (essai + 1))
                    continue
                data = r.json()
                if data.get("code") == "TooBig":
                    raise OsrmTooBig()
                if r.status_code != 200 or data.get("code") != "Ok":
                    raise RuntimeError(data.get("message") or data.get("code") or r.status_code)
                return data
            except OsrmTooBig:
                raise
            except Exception as e:                          # réseau, JSON invalide, etc.
                derniere = e
                time.sleep(1 + essai)
        raise RuntimeError(f"OSRM injoignable : {derniere}")

    @staticmethod
    def _chemin(points) -> str:
        return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)

    def table(self, sources: list, destinations: list):
        pts = list(sources) + list(destinations)
        ns = len(sources)
        url = (f"{self.base}/table/v1/driving/{self._chemin(pts)}"
               f"?sources={';'.join(map(str, range(ns)))}"
               f"&destinations={';'.join(map(str, range(ns, len(pts))))}&annotations=duration,distance")
        data = self._get(url)
        return data["durations"], data["distances"]

    def route(self, points: list):
        """Tracé réel d'une tournée : (liste de (lat, lon), durée en s, distance en m)."""
        url = f"{self.base}/route/v1/driving/{self._chemin(points)}?overview=full&geometries=geojson&steps=false"
        r = self._get(url)["routes"][0]
        return [(lat, lon) for lon, lat in r["geometry"]["coordinates"]], r["duration"], r["distance"]


class TravelModel:
    """Matrices de durées (s) et de distances (m) de la journée, avec cache entre les jours."""
    DEPOT = "DEPOT"

    def __init__(self, params: Params):
        self.p = params
        self.client = OsrmClient(params)
        self.cache: dict = {}
        self.known: set = set()
        self.mode = "estimation"
        self.erreur = ""

    def _estimation(self, lats, lons):
        dist_km = haversine_km(lats[:, None], lons[:, None], lats[None, :], lons[None, :]) * self.p.facteur_detour
        dur = np.rint(dist_km / self.p.vitesse_kmh * 3600)
        return dur, np.rint(dist_km * 1000)

    def _remplir(self, k: list, pts: list) -> None:
        while True:
            try:
                n, B = len(k), self.client.block
                for i in range(0, n, B):
                    src = range(i, min(i + B, n))
                    for j in range(0, n, B):
                        dst = range(j, min(j + B, n))
                        if all((k[a], k[b]) in self.cache for a in src for b in dst if a != b):
                            continue
                        durs, dists = self.client.table([pts[a] for a in src], [pts[b] for b in dst])
                        for ia, a in enumerate(src):
                            for ib, b in enumerate(dst):
                                if a == b:
                                    continue
                                if durs[ia][ib] is None or dists[ia][ib] is None:
                                    raise RuntimeError("trajet introuvable entre deux points")
                                self.cache[(k[a], k[b])] = (durs[ia][ib], dists[ia][ib])
                self.known.update(k)
                return
            except OsrmTooBig:
                if self.client.block <= 5:
                    raise RuntimeError("OSRM refuse les requêtes (trop de points)")
                self.client.block //= 2

    def build(self, keys: list, lats, lons):
        """Retourne (durées en secondes avec coefficient de circulation, distances en mètres)."""
        lats, lons = np.asarray(lats, float), np.asarray(lons, float)
        n = len(keys)
        if not self.p.use_osrm:
            self.mode = "estimation"
            return self._estimation(lats, lons)
        ordre = sorted(range(n), key=lambda i: keys[i] not in self.known)     # nœuds déjà connus d'abord
        k = [keys[i] for i in ordre]
        pts = [(lats[i], lons[i]) for i in ordre]
        try:
            self._remplir(k, pts)
        except Exception as e:
            self.mode, self.erreur = "estimation", str(e)
            return self._estimation(lats, lons)
        self.mode, self.erreur = "OSRM", ""
        dur, dist = np.zeros((n, n)), np.zeros((n, n))
        for a in range(n):
            for b in range(n):
                if a != b:
                    d, m = self.cache[(k[a], k[b])]
                    dur[ordre[a], ordre[b]] = d * self.p.coef_circulation
                    dist[ordre[a], ordre[b]] = m
        return np.rint(dur), np.rint(dist)

    def nettoyer(self, cles_actives: set) -> None:
        cles_actives = set(cles_actives) | {self.DEPOT}
        self.cache = {c: v for c, v in self.cache.items() if c[0] in cles_actives and c[1] in cles_actives}
        self.known &= cles_actives


# ----------------------------------------------------------------------------
# Planification quotidienne
# ----------------------------------------------------------------------------
@dataclass
class Route:
    camion_id: str
    rotation: int = 1                                  # 1re, 2e, 3e tournée du camion dans la journée
    clients: list = field(default_factory=list)
    arrets: list = field(default_factory=list)
    charge: int = 0
    capacite: int = 0
    distance_km: float = 0.0
    minutes_tournee: int = 0                           # trajets + livraisons de cette tournée
    debut_actif: float = 0.0                           # minutes d'activité écoulées au départ
    retour_actif: float = 0.0                          # minutes d'activité écoulées au retour
    depart_min: int = 0                                # heure de départ (minutes depuis minuit)
    retour_min: int = 0                                # heure de retour (minutes depuis minuit)
    conducteur_id: str | None = None
    geometrie: list | None = None                      # tracé OSRM [(lat, lon), ...]
    geometrie_osrm: bool = False


class DailyPlanner:
    """Choisit quels clients livrer, avec quels camions et en combien de rotations (OR-Tools)."""
    PAUSE_APRES_ACTIF_MIN = 240                        # la pause tombe après 4h d'activité (vers 12h)

    def __init__(self, params: Params):
        self.p = params
        self.moteur = "OR-Tools"

    def horloge(self, actif_min: float) -> float:
        """Minutes depuis minuit pour un instant situé à `actif_min` minutes d'activité après 8h00."""
        decalage = self.p.pause_min if actif_min >= self.PAUSE_APRES_ACTIF_MIN else 0
        return self.p.debut_min + actif_min + decalage

    @staticmethod
    def penalite(client: Client, jour: int) -> int:
        """Coût de NE PAS livrer un client : ordre strict prioritaires > anciens > du jour."""
        age = min(max(jour - client.date_commande, 0), 300)
        if client.prioritaire:
            return 10**12 + age * 10**9
        if age > 0:
            return 10**9 + age * 10**6
        return 10**6

    def plan(self, clients: list, camions: list, n_conducteurs: int, jour: int, travel: TravelModel) -> list:
        """Planifie la journée en plusieurs passes : 1re tournée de chaque camion, puis 2e tournée avec le
        temps restant (après rechargement), etc. Chaque passe est un VRP résolu avec OR-Tools."""
        p = self.p
        if not clients or not camions or n_conducteurs <= 0:
            return []

        limite_min = p.minutes_actives_max
        camions = sorted(camions, key=lambda c: (-c.capacite, c.id))[:n_conducteurs]   # 1 conducteur par camion
        cap_max = max(c.capacite for c in camions)
        ordre = sorted(clients, key=lambda c: (not c.prioritaire, c.date_commande, c.id))
        cand = [c for c in ordre if c.volume_restant <= cap_max][: p.max_candidats]
        if not cand:
            return []

        keys = [TravelModel.DEPOT] + [c.id for c in cand]
        dur, dist = travel.build(keys, [p.entrepot_lat] + [c.lat for c in cand], [p.entrepot_lon] + [c.lon for c in cand])
        service = np.array([0] + [p.duree_service_min(len(c.colis_restants)) * 60 for c in cand])
        # écarte les clients impossibles à servir dans la journée
        ok = [0] + [i for i in range(1, len(cand) + 1) if dur[0][i] + service[i] + dur[i][0] <= limite_min * 60]
        if len(ok) == 1:
            return []
        cand = [cand[i - 1] for i in ok[1:]]
        dur, dist, service = dur[np.ix_(ok, ok)], dist[np.ix_(ok, ok)], service[ok]
        volumes = [0] + [c.volume_restant for c in cand]

        restants = set(range(1, len(cand) + 1))
        dispo = {c.id: 0.0 for c in camions}            # minutes d'activité écoulées quand le camion est de nouveau prêt
        nb_tournees = {c.id: 0 for c in camions}
        routes = []
        R = max(1, p.max_rotations)
        for passe in range(R):
            actifs = [c for c in camions if limite_min - dispo[c.id] > 15]
            if not restants or not actifs:
                break
            sous = self._resoudre(sorted(restants), actifs, dispo, dur, dist, service, volumes, cand, jour,
                                  max(2, p.temps_solveur_s // R))
            for camion, nodes in sous:
                nb_tournees[camion.id] += 1
                route = self._construire(camion, nb_tournees[camion.id], nodes, cand, dur, dist, service, volumes,
                                         dispo[camion.id])
                routes.append(route)
                dispo[camion.id] = route.retour_actif + p.rechargement_min
                restants -= set(nodes)
        return routes

    def _resoudre(self, noeuds, camions, dispo, dur, dist, service, volumes, cand, jour, temps_s):
        """Un VRP : retourne [(camion, [nœuds dans l'ordre])]. `noeuds` : indices de clients dans les matrices."""
        p = self.p
        if p.solveur == "heuristique" or (p.solveur == "auto" and not ortools_disponible()):
            self.moteur = "heuristique de secours"
            return self._resoudre_heuristique(noeuds, camions, dispo, dur, dist, service, volumes, cand, jour)
        self.moteur = "OR-Tools"
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
        idx_global = [0] + list(noeuds)                  # nœud local -> nœud global
        d, m, sv = dur[np.ix_(idx_global, idx_global)], dist[np.ix_(idx_global, idx_global)], service[idx_global]
        dur_l, dist_l, svc_l = d.astype(int).tolist(), m.astype(int).tolist(), sv.astype(int).tolist()
        vol = [volumes[i] for i in idx_global]

        mgr = pywrapcp.RoutingIndexManager(len(idx_global), len(camions), 0)
        routing = pywrapcp.RoutingModel(mgr)
        cout_cb = routing.RegisterTransitCallback(lambda i, j: dist_l[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
        routing.SetArcCostEvaluatorOfAllVehicles(cout_cb)                      # on minimise les kilomètres
        temps_cb = routing.RegisterTransitCallback(
            lambda i, j: dur_l[mgr.IndexToNode(i)][mgr.IndexToNode(j)] + svc_l[mgr.IndexToNode(i)])
        reste_s = [int((p.minutes_actives_max - dispo[c.id]) * 60) for c in camions]   # temps restant de chaque camion
        routing.AddDimensionWithVehicleCapacity(temps_cb, 0, reste_s, True, "Temps")
        vol_cb = routing.RegisterUnaryTransitCallback(lambda i: vol[mgr.IndexToNode(i)])
        routing.AddDimensionWithVehicleCapacity(vol_cb, 0, [c.capacite for c in camions], True, "Volume")
        for v in range(len(camions)):                                           # moins de camions à égalité de kilomètres
            routing.SetFixedCostOfVehicle(3000, v)
        for i, g in enumerate(noeuds, start=1):                                # clients facultatifs, pénalité par priorité
            routing.AddDisjunction([mgr.NodeToIndex(i)], self.penalite(cand[g - 1], jour))

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromSeconds(temps_s)
        sol = routing.SolveWithParameters(params)
        if sol is None:
            return []
        out = []
        for v, camion in enumerate(camions):
            idx, nodes = routing.Start(v), []
            while not routing.IsEnd(idx):
                n = mgr.IndexToNode(idx)
                if n != 0:
                    nodes.append(idx_global[n])
                idx = sol.Value(routing.NextVar(idx))
            if nodes:
                out.append((camion, nodes))
        return out

    def _resoudre_heuristique(self, noeuds, camions, dispo, dur, dist, service, volumes, cand, jour):
        """Solveur de secours sans OR-Tools : insertion au moindre coût dans l'ordre strict de priorité,
        puis amélioration 2-opt de chaque tournée. Respecte capacité, temps restant et priorités."""
        p = self.p
        reste = {c.id: (p.minutes_actives_max - dispo[c.id]) * 60 for c in camions}
        routes = {c.id: [] for c in camions}
        charge = {c.id: 0 for c in camions}
        temps = {c.id: 0.0 for c in camions}                 # trajets + livraisons de la tournée
        OUVERTURE = 8000                                      # petit coût pour ouvrir une tournée (moins de camions)
        ordre = sorted(noeuds, key=lambda g: (-self.penalite(cand[g - 1], jour), g))

        def inserer(g) -> bool:
            meilleur = None
            for c in camions:
                if charge[c.id] + volumes[g] > c.capacite:
                    continue
                r = routes[c.id]
                if not r:
                    dt = dur[0][g] + service[g] + dur[g][0]
                    if dt <= reste[c.id]:
                        dd = dist[0][g] + dist[g][0] + OUVERTURE
                        if meilleur is None or dd < meilleur[0]:
                            meilleur = (dd, c, 0, dt)
                    continue
                seq = [0] + r + [0]
                for pos in range(len(seq) - 1):
                    a, b = seq[pos], seq[pos + 1]
                    dt = dur[a][g] + dur[g][b] - dur[a][b] + service[g]
                    if temps[c.id] + dt > reste[c.id]:
                        continue
                    dd = dist[a][g] + dist[g][b] - dist[a][b]
                    if meilleur is None or dd < meilleur[0]:
                        meilleur = (dd, c, pos, dt)
            if meilleur is None:
                return False
            _, c, pos, dt = meilleur
            routes[c.id].insert(pos, g)
            charge[c.id] += volumes[g]
            temps[c.id] += dt
            return True

        non_places = [g for g in ordre if not inserer(g)]

        def cout(seq, M):
            return sum(M[seq[i]][seq[i + 1]] for i in range(len(seq) - 1))

        def deplacements(duree_max_s: float = 3.0) -> None:
            """Déplace un client vers une meilleure position (même tournée ou autre) tant que ça réduit les km."""
            fin = time.time() + duree_max_s
            ameliore = True
            while ameliore and time.time() < fin:
                ameliore = False
                for c1 in camions:
                    r1 = routes[c1.id]
                    for idx in range(len(r1) - 1, -1, -1):
                        if idx >= len(r1):
                            continue
                        g = r1[idx]
                        seq1 = [0] + r1 + [0]
                        a, b = seq1[idx], seq1[idx + 2]
                        gain_d = dist[a][g] + dist[g][b] - dist[a][b]
                        gain_t = dur[a][g] + dur[g][b] - dur[a][b] + service[g]
                        meilleur = None
                        for c2 in camions:
                            if c2.id != c1.id and charge[c2.id] + volumes[g] > c2.capacite:
                                continue
                            base_seq = seq1[:idx + 1] + seq1[idx + 2:] if c2.id == c1.id else [0] + routes[c2.id] + [0]
                            t2 = (temps[c1.id] - gain_t) if c2.id == c1.id else temps[c2.id]
                            for pos in range(len(base_seq) - 1):
                                x, y = base_seq[pos], base_seq[pos + 1]
                                dt = dur[x][g] + dur[g][y] - dur[x][y] + service[g]
                                if t2 + dt > reste[c2.id]:
                                    continue
                                dd = dist[x][g] + dist[g][y] - dist[x][y]
                                if dd < gain_d - 1 and (meilleur is None or dd < meilleur[0]):
                                    meilleur = (dd, c2, pos, dt, base_seq)
                        if meilleur is not None:
                            dd, c2, pos, dt, base_seq = meilleur
                            r1.pop(idx)
                            charge[c1.id] -= volumes[g]
                            temps[c1.id] -= gain_t
                            if c2.id == c1.id:
                                base_seq.insert(pos + 1, g)
                                routes[c1.id] = base_seq[1:-1]
                                r1 = routes[c1.id]                # la liste a été remplacée
                            else:
                                routes[c2.id].insert(pos, g)
                            charge[c2.id] += volumes[g]
                            temps[c2.id] += dt
                            ameliore = True

        deplacements()
        for c in camions:                                     # amélioration 2-opt
            r = routes[c.id]
            if len(r) < 3:
                continue
            seq = [0] + r + [0]
            svc = sum(service[g] for g in r)
            amelioration, passes = True, 0
            while amelioration and passes < 15:
                amelioration, passes = False, passes + 1
                base = cout(seq, dist)
                for i in range(1, len(seq) - 2):
                    for j in range(i + 1, len(seq) - 1):
                        nouveau = seq[:i] + seq[i:j + 1][::-1] + seq[j + 1:]
                        if cout(nouveau, dist) < base - 1 and cout(nouveau, dur) + svc <= reste[c.id]:
                            seq, base, amelioration = nouveau, cout(nouveau, dist), True
            routes[c.id] = seq[1:-1]
            temps[c.id] = cout(seq, dur) + svc
        for g in non_places:                                  # les tournées raccourcies peuvent accueillir d'autres clients
            inserer(g)
        deplacements(1.5)
        return [(c, routes[c.id]) for c in camions if routes[c.id]]

    def _construire(self, camion, rotation, nodes, cand, dur, dist, service, volumes, offset) -> Route:
        p = self.p
        r = Route(camion_id=camion.id, rotation=rotation, capacite=camion.capacite, debut_actif=offset)
        actif, prev, km = offset, 0, 0.0
        for ordre, node in enumerate(nodes, start=1):
            actif += dur[prev][node] / 60
            km += dist[prev][node] / 1000
            client = cand[node - 1]
            arrivee = actif
            livraisons = [(c.id, hhmm(self.horloge(arrivee + p.service_min_par_colis + (j - 1) * p.service_min_colis_suppl)))
                          for j, c in enumerate(client.colis_restants, start=1)]
            actif += service[node] / 60
            r.clients.append(client.id)
            r.arrets.append({"ordre": ordre, "client_id": client.id, "arrivee": hhmm(self.horloge(arrivee)),
                             "nb_colis": len(client.colis_restants), "livraisons": livraisons})
            r.charge += volumes[node]
            prev = node
        actif += dur[prev][0] / 60
        km += dist[prev][0] / 1000
        r.distance_km = round(float(km), 1)
        r.retour_actif = actif
        r.minutes_tournee = int(round(actif - offset))
        r.depart_min = int(round(self.horloge(offset)))
        r.retour_min = int(round(p.debut_min + actif + (p.pause_min if actif > self.PAUSE_APRES_ACTIF_MIN else 0)))
        return r


# ----------------------------------------------------------------------------
# Simulation
# ----------------------------------------------------------------------------
@dataclass
class RapportJour:
    jour: int
    nouveaux_colis: int
    a_livrer: int                  # colis en attente avant les tournées
    livres: int
    restants: int
    prioritaires_a_livrer: int
    prioritaires_livres: int
    routes: list
    pannes: list
    conducteurs_utilises: list
    statuts: dict                  # id conducteur -> statut du jour
    retours: dict                  # id conducteur -> heure de retour (minutes)
    colis_par_conducteur: dict
    distance_totale_km: float
    age_moyen_restants: float
    n_camions: int                 # camions possédés
    n_conducteurs: int             # conducteurs employés
    mode_trajet: str               # "OSRM" ou "estimation"
    erreur_trajet: str
    finance: dict
    moteur: str = ""
    secteurs: str = ""


class Simulation:
    def __init__(self, params: Params | None = None, data_dir: str | Path = "data"):
        self.p = params or Params()
        self.rng = np.random.default_rng(self.p.seed)
        self._rnd = random.Random(self.p.seed)
        self.pool = AddressPool(self.p, Path(data_dir))
        self.planner = DailyPlanner(self.p)
        self.travel = TravelModel(self.p)
        self.jour = 0                       # 0 = premier lundi
        self.journee_faite = False
        self.clients: dict[str, Client] = {}
        self.rapports: list[RapportJour] = []
        self.livraisons: list[dict] = []
        self._n_client = self._n_colis = self._n_camion = self._n_conducteur = 0
        self.camions: list[Camion] = []
        self.conducteurs: list[Conducteur] = []
        self.ajuster_ressources(self.p.n_grands_camions, self.p.n_petits_camions, self.p.n_conducteurs, initial=True)

    # ---- ressources (modifiables chaque jour)
    @property
    def n_grands(self) -> int:
        return sum(1 for c in self.camions if c.taille == "grand")

    @property
    def n_petits(self) -> int:
        return sum(1 for c in self.camions if c.taille == "petit")

    def _nouveau_camion(self, taille: str) -> Camion:
        self._n_camion += 1
        r = self._rnd
        plaque = (r.choice(string.ascii_uppercase) + r.choice(string.ascii_uppercase) + "-" + f"{r.randint(100, 999)}-"
                  + r.choice(string.ascii_uppercase) + r.choice(string.ascii_uppercase))
        cap = self.p.capacite_grand if taille == "grand" else self.p.capacite_petit
        return Camion(id=f"CAM-{self._n_camion}", plaque=plaque, taille=taille, capacite=cap)

    def _nouveau_conducteur(self, initial: bool) -> Conducteur:
        self._n_conducteur += 1
        i = self._n_conducteur - 1
        repos = [(5, 6), (6, 0), (0, 1), (2, 3), (3, 4), (1, 2)]       # repos décalés entre conducteurs
        d = Conducteur(id=f"DRV-{self._n_conducteur}", nom=self._rnd.choice(NOMS), prenom=self._rnd.choice(PRENOMS),
                       jours_repos=repos[i % len(repos)], echeance_conges=27 - 5 * (i % 5))
        if not initial:
            d.conges_payes_pris = self.p.conges_min_mois      # embauché en cours de mois : congés du mois déjà couverts
        return d

    def ajuster_ressources(self, n_grands: int, n_petits: int, n_conducteurs: int, initial: bool = False) -> None:
        for taille, n in (("grand", n_grands), ("petit", n_petits)):
            courants = [c for c in self.camions if c.taille == taille]
            for _ in range(max(0, n - len(courants))):
                self.camions.append(self._nouveau_camion(taille))
            for c in courants[max(n, 0):]:
                self.camions.remove(c)
        self.camions.sort(key=lambda c: (c.taille != "grand", int(c.id.split("-")[1])))
        n_conducteurs = max(1, n_conducteurs)
        while len(self.conducteurs) < n_conducteurs:
            self.conducteurs.append(self._nouveau_conducteur(initial))
        while len(self.conducteurs) > n_conducteurs:
            self.conducteurs.pop()

    # ---- calendrier
    @property
    def jour_semaine(self) -> int:
        return self.jour % 7

    @property
    def jour_du_mois(self) -> int:
        return self.jour % self.p.jours_par_mois

    @property
    def libelle_jour(self) -> str:
        return (f"Jour {self.jour + 1} — {JOURS[self.jour_semaine]} "
                f"(semaine {self.jour // 7 % 4 + 1}, mois {self.jour // self.p.jours_par_mois + 1})")

    # ---- génération des clients et colis
    def _generer(self, n_grand: int, n_moyen: int, n_petit: int) -> int:
        tailles = ["grand"] * n_grand + ["moyen"] * n_moyen + ["petit"] * n_petit
        self.rng.shuffle(tailles)
        adresses = self.pool.sample(len(tailles), self.rng)   # au plus 1 adresse par client
        k = 0
        while tailles:
            nb = 1
            if len(tailles) > 1 and self.rng.random() < self.p.p_deuxieme_colis:
                nb = 2
                if len(tailles) > 2 and self.rng.random() < self.p.p_troisieme_colis:
                    nb = 3
            lat, lon, adr = adresses[k]
            k += 1
            self._n_client += 1
            client = Client(id=f"CLI-{self._n_client:04d}", nom=NOMS[self.rng.integers(len(NOMS))],
                            prenom=PRENOMS[self.rng.integers(len(PRENOMS))],
                            prioritaire=bool(self.rng.random() < self.p.p_prioritaire),
                            lat=float(lat), lon=float(lon), adresse=str(adr),
                            secteur=secteur_de(float(lat), float(lon), self.p.entrepot_lat, self.p.entrepot_lon),
                            distance_km=float(haversine_km(self.p.entrepot_lat, self.p.entrepot_lon, float(lat), float(lon))))
            for _ in range(nb):
                self._n_colis += 1
                client.colis.append(Colis(id=f"COL-{self._n_colis:05d}", client_id=client.id,
                                          date_commande=self.jour, taille=tailles.pop(), lat=client.lat, lon=client.lon))
            self.clients[client.id] = client
        return n_grand + n_moyen + n_petit

    # ---- conducteurs
    def _conge_impose(self, d: Conducteur) -> bool:
        """Vrai si le conducteur doit prendre congé aujourd'hui pour atteindre ses 3 jours minimum."""
        besoin = self.p.conges_min_mois - d.conges_payes_pris
        if besoin <= 0:
            return False
        dim, dow = self.jour_du_mois, self.jour_semaine
        dispo = sum(1 for k in range(dim, d.echeance_conges + 1) if (dow + k - dim) % 7 not in d.jours_repos)
        return dispo <= besoin

    def planning_semaine(self, d: Conducteur) -> list:
        """[(jour, statut)] pour la semaine en cours : passé = réalisé, futur = repos ou disponible."""
        debut = self.jour - self.jour_semaine
        out = []
        for k in range(7):
            j = debut + k
            if j in d.historique:
                statut = d.historique[j]
            elif j < self.jour or (j == self.jour and self.journee_faite):
                statut = "—"
            elif k in d.jours_repos:
                statut = "Repos hebdo"
            else:
                statut = "Disponible" if j > self.jour else "À planifier"
            out.append((JOURS[k], statut))
        return out

    # ---- journée
    def run_day(self, n_grand: int, n_moyen: int, n_petit: int) -> RapportJour:
        p = self.p
        nouveaux = self._generer(n_grand, n_moyen, n_petit)

        pannes = []
        for c in self.camions:                       # panne tirée chaque matin
            c.en_panne = bool(self.rng.random() < p.p_panne)
            c.conducteur_id = None
            if c.en_panne:
                pannes.append(c.id)

        statuts, eligibles = {}, []
        for d in self.conducteurs:
            if d.est_repos(self.jour_semaine):
                statuts[d.id] = "Repos hebdo"
            elif self._conge_impose(d):
                d.conges_payes_pris += 1
                statuts[d.id] = "Congé payé (imposé)"
            else:
                eligibles.append(d)
        eligibles.sort(key=lambda d: (-d.conges_payes_pris, d.id))     # ceux qui ont déjà leurs congés travaillent d'abord

        secteurs_jour = self.secteurs_du_jour()
        en_attente = [c for c in self.clients.values() if c.colis_restants
                      and (not p.livraison_par_secteur or c.prioritaire or c.secteur in secteurs_jour)]
        a_livrer = sum(len(c.colis_restants) for c in en_attente)
        prio_avant = sum(len(c.colis_restants) for c in en_attente if c.prioritaire)
        camions_ok = [c for c in self.camions if not c.en_panne]
        routes = self.planner.plan(en_attente, camions_ok, len(eligibles), self.jour, self.travel)

        # un conducteur par camion utilisé
        par_camion = {c.id: c for c in self.camions}
        ordre_camions = list(dict.fromkeys(r.camion_id for r in routes))
        retours, utilises, colis_par_cond, km, prio_livres = {}, [], {}, 0.0, 0
        livres_ids = set()
        affectation = dict(zip(ordre_camions, eligibles))
        for route in routes:
            camion, driver = par_camion[route.camion_id], affectation[route.camion_id]
            camion.conducteur_id = driver.id
            route.conducteur_id = driver.id
            statuts[driver.id] = "Travail"
            retours[driver.id] = max(retours.get(driver.id, 0), route.retour_min)
            if driver.id not in utilises:
                utilises.append(driver.id)
            km += route.distance_km
            for arret in route.arrets:
                client = self.clients[arret["client_id"]]
                heures = dict(arret["livraisons"])
                for colis in client.colis_restants:
                    colis.livre = True
                    livres_ids.add(colis.id)
                    colis_par_cond[driver.id] = colis_par_cond.get(driver.id, 0) + 1
                    prio_livres += 1 if client.prioritaire else 0
                    self.livraisons.append({
                        "Jour": self.jour + 1, "ID colis": colis.id, "ID client": client.id,
                        "Client": f"{client.prenom} {client.nom}", "Prioritaire": "Oui" if client.prioritaire else "Non",
                        "Taille": colis.taille, "Volume": colis.volume, "ID camion": camion.id, "Plaque": camion.plaque,
                        "ID conducteur (livreur)": driver.id, "Conducteur": f"{driver.prenom} {driver.nom}",
                        "Secteur": client.secteur, "Rotation": route.rotation, "Ordre d'arrêt": arret["ordre"],
                        "Heure de livraison": heures.get(colis.id, ""),
                    })

        for d in eligibles:                          # conducteurs non utilisés -> congé
            if d.id in utilises:
                continue
            if d.conges_payes_pris < p.conges_min_mois:
                d.conges_payes_pris += 1
                statuts[d.id] = "Congé payé"
            else:
                d.conges_offerts += 1
                statuts[d.id] = "Congé offert"
        for d in self.conducteurs:
            d.historique[self.jour] = statuts[d.id]

        restants_liste = [c for cl in self.clients.values() for c in cl.colis_restants]
        restants = len(restants_liste)
        livres_jour = sum(colis_par_cond.values())
        age = float(np.mean([self.jour - c.date_commande for c in restants_liste])) if restants_liste else 0.0

        # recettes et coûts de la journée
        marge = express = zone = 0.0
        for cl in self.clients.values():
            for c in cl.colis:
                if c.id in livres_ids:
                    marge += p.marge_colis(c.taille)
                    express += p.supplement_express if cl.prioritaire else 0.0
                    zone += p.supplement_zone_eloignee if cl.distance_km > p.zone_eloignee_km else 0.0
        finance = {
            "Marge sur colis livrés": marge,
            "Supplément express": express,
            "Supplément zone éloignée": zone,
            "Salaires": len(self.conducteurs) * p.cout_conducteur_jour,
            "Entrepôt : employé": p.cout_employe_entrepot_jour,
            "Entrepôt : location, électricité, charges": p.cout_location_entrepot_jour,
            "Camions (coût journalier)": sum(p.cout_camion(c.taille) for c in self.camions),
            "Entretien préventif des camions": len(self.camions) * p.cout_entretien_camion_jour,
            "Carburant": km * p.cout_km,
            "Réparations (pannes)": len(pannes) * p.cout_panne,
            "Stockage": restants * p.cout_stockage_colis_jour,
            "Pénalités de retard": restants * p.cout_retard_colis_jour,
        }
        recettes = ("Marge sur colis livrés", "Supplément express", "Supplément zone éloignée")
        finance["Total des recettes"] = marge + express + zone
        finance["Total des coûts"] = sum(v for k, v in finance.items() if k not in recettes and k != "Total des recettes")
        finance["Résultat net"] = finance["Total des recettes"] - finance["Total des coûts"]

        self.travel.nettoyer({c.id for c in self.clients.values() if c.colis_restants})
        rapport = RapportJour(
            jour=self.jour, nouveaux_colis=nouveaux, a_livrer=a_livrer, livres=livres_jour, restants=restants,
            prioritaires_a_livrer=prio_avant, prioritaires_livres=prio_livres, routes=routes, pannes=pannes,
            conducteurs_utilises=utilises, statuts=statuts, retours=retours, colis_par_conducteur=colis_par_cond,
            distance_totale_km=round(km, 1), age_moyen_restants=round(age, 2), n_camions=len(self.camions),
            n_conducteurs=len(self.conducteurs), mode_trajet=self.travel.mode, erreur_trajet=self.travel.erreur,
            finance=finance, moteur=self.planner.moteur,
            secteurs=" + ".join(secteurs_jour) if p.livraison_par_secteur else "Tous")
        self.rapports.append(rapport)
        self.journee_faite = True
        return rapport

    def jour_suivant(self) -> None:
        self.jour += 1
        self.journee_faite = False
        if self.jour % self.p.jours_par_mois == 0:          # nouveau mois : compteurs de congés remis à zéro
            for d in self.conducteurs:
                d.conges_payes_pris = 0
                d.conges_offerts = 0

    def secteurs_du_jour(self) -> list:
        """2 secteurs voisins par jour (Nord+Est, Est+Sud, Sud+Ouest, Ouest+Nord) : chaque secteur est livré 1 jour sur 2."""
        i = self.jour % len(SECTEURS)
        return [SECTEURS[(i + k) % len(SECTEURS)] for k in range(self.p.secteurs_par_jour)]

    # ---- flotte recommandée (option « flotte adaptée au volume »)
    def minutes_par_colis(self) -> float:
        """Temps de conducteur par colis livré, observé sur les derniers jours (10 min par défaut)."""
        recents = [r for r in self.rapports[-5:] if r.livres > 0]
        if not recents:
            return 8.0
        minutes = sum(x.minutes_tournee for r in recents for x in r.routes)
        return max(minutes / sum(r.livres for r in recents), 3.0)

    def flotte_recommandee(self, n_nouveaux: int, volume_nouveaux: int) -> tuple:
        """(grands camions, petits camions, conducteurs) suffisants pour les colis en attente + ceux du jour."""
        p = self.p
        attente = [c for cl in self.clients.values() for c in cl.colis_restants]
        n_colis = len(attente) + n_nouveaux
        volume = sum(c.volume for c in attente) + volume_nouveaux
        if p.livraison_par_secteur:                                           # seule une partie des colis est due chaque jour
            part = p.secteurs_par_jour / len(SECTEURS)
            facteur = part + p.p_prioritaire * (1 - part)
            n_colis, volume = int(n_colis * facteur), int(volume * facteur)
        if n_colis == 0:
            return 0, 1, 1
        par_temps = -(-int(n_colis * self.minutes_par_colis()) // p.minutes_actives_max)
        par_volume = -(-int(volume) // (p.capacite_petit * 2))               # environ 2 tournées par camion
        camions = int(min(max(1, par_temps, par_volume), 30))
        reste = volume / 2 - camions * p.capacite_petit                     # volume que les petits camions ne couvrent pas
        grands = 0 if reste <= 0 else min(camions, -(-int(reste) // (p.capacite_grand - p.capacite_petit)))
        conducteurs = -(-camions * 7 // 5)                                  # chaque conducteur travaille 5 jours sur 7
        return grands, camions - grands, max(1, conducteurs)

    # ---- tracé réel des routes (OSRM), calculé à la demande
    def charger_traces(self, rapport: RapportJour) -> None:
        for r in rapport.routes:
            if r.geometrie is not None:
                continue
            pts = [(self.p.entrepot_lat, self.p.entrepot_lon)]
            pts += [(self.clients[a["client_id"]].lat, self.clients[a["client_id"]].lon) for a in r.arrets]
            pts.append(pts[0])
            try:
                if not self.p.use_osrm:
                    raise RuntimeError("OSRM désactivé")
                r.geometrie, _, _ = self.travel.client.route(pts)
                r.geometrie_osrm = True
            except Exception:
                r.geometrie, r.geometrie_osrm = pts, False      # secours : traits droits

    # ---- accès pratiques pour l'interface
    @property
    def dernier_rapport(self) -> RapportJour | None:
        return self.rapports[-1] if self.rapports else None

    def colis_restants_df(self) -> pd.DataFrame:
        rows = []
        for cl in self.clients.values():
            for c in cl.colis_restants:
                rows.append({"ID colis": c.id, "ID client": cl.id, "Client": f"{cl.prenom} {cl.nom}",
                             "Prioritaire": "Oui" if cl.prioritaire else "Non", "Taille": c.taille, "Volume": c.volume,
                             "Date de commande (jour)": c.date_commande + 1, "Ancienneté (jours)": self.jour - c.date_commande,
                             "Secteur": cl.secteur, "Distance de l'entrepôt (km)": round(cl.distance_km, 1),
                             "Adresse": cl.adresse, "Latitude": round(cl.lat, 5), "Longitude": round(cl.lon, 5)})
        df = pd.DataFrame(rows)
        if not df.empty:
            df["_o"] = df["Prioritaire"].eq("Non")
            df = df.sort_values(["_o", "Date de commande (jour)", "ID colis"]).drop(columns="_o")
        return df

    def bilan_df(self) -> pd.DataFrame:
        """Une ligne par journée : activité, indicateurs et finances."""
        rows = []
        for r in self.rapports:
            camions_utilises = len({x.camion_id for x in r.routes})
            charge = sum(x.charge for x in r.routes)
            capacite = sum(x.capacite for x in r.routes)
            actif = {}
            for x in r.routes:
                actif[x.camion_id] = max(actif.get(x.camion_id, 0), x.retour_actif)
            row = {
                "Jour": r.jour + 1, "Jour de la semaine": JOURS[r.jour % 7], "Nouveaux colis": r.nouveaux_colis,
                "Colis à livrer": r.a_livrer, "Colis livrés": r.livres, "Colis restants": r.restants,
                "Taux de service (%)": round(100 * r.livres / r.a_livrer, 1) if r.a_livrer else 100.0,
                "Prioritaires livrés (%)": round(100 * r.prioritaires_livres / r.prioritaires_a_livrer, 1)
                if r.prioritaires_a_livrer else 100.0,
                "Camions utilisés": camions_utilises, "Conducteurs utilisés": len(r.conducteurs_utilises),
                "Tournées (rotations)": len(r.routes), "Distance (km)": r.distance_totale_km,
                "Km par colis": round(r.distance_totale_km / r.livres, 2) if r.livres else 0.0,
                "Remplissage des camions (%)": round(100 * charge / capacite, 1) if capacite else 0.0,
                "Utilisation des conducteurs (%)": round(100 * sum(actif.values()) /
                                                          (self.p.minutes_actives_max * max(len(actif), 1)), 1) if actif else 0.0,
                "Âge moyen des restants (jours)": r.age_moyen_restants, "Pannes": len(r.pannes),
                "Secteurs du jour": r.secteurs, "Trajets": r.mode_trajet, "Moteur": r.moteur,
            }
            row.update({k: round(v, 2) for k, v in r.finance.items()})
            row["Coût par colis livré"] = round(r.finance["Total des coûts"] / r.livres, 2) if r.livres else None
            rows.append(row)
        return pd.DataFrame(rows)

    def tournees_df(self) -> pd.DataFrame:
        plaques = {c.id: c.plaque for c in self.camions}
        rows = []
        for r in self.rapports:
            for x in r.routes:
                rows.append({"Jour": r.jour + 1, "Camion": x.camion_id, "Plaque": plaques.get(x.camion_id, ""),
                             "Conducteur": x.conducteur_id, "Rotation": x.rotation, "Arrêts": len(x.arrets),
                             "Colis": sum(a["nb_colis"] for a in x.arrets), "Charge (volume)": x.charge,
                             "Capacité": x.capacite, "Distance (km)": x.distance_km,
                             "Durée de la tournée (min)": x.minutes_tournee, "Départ": hhmm(x.depart_min),
                             "Retour": hhmm(x.retour_min)})
        return pd.DataFrame(rows)

    def conducteurs_df(self) -> pd.DataFrame:
        rows = []
        for d in self.conducteurs:
            row = {"Conducteur": f"{d.id} — {d.prenom} {d.nom}", "Congés payés pris (mois en cours)": d.conges_payes_pris,
                   "Congés offerts (mois en cours)": d.conges_offerts}
            for j in sorted(d.historique):
                row[f"Jour {j + 1}"] = d.historique[j]
            rows.append(row)
        return pd.DataFrame(rows)

    def exporter_excel(self) -> bytes:
        """Classeur Excel : bilan par jour, colis livrés et restants, conducteurs, tournées, paramètres."""
        buf = io.BytesIO()
        params = pd.DataFrame({"Paramètre": list(asdict(self.p).keys()), "Valeur": [str(v) for v in asdict(self.p).values()]})
        feuilles = {"Bilan par jour": self.bilan_df(), "Colis livrés": pd.DataFrame(self.livraisons),
                    "Colis restants": self.colis_restants_df(), "Conducteurs": self.conducteurs_df(),
                    "Tournées": self.tournees_df(), "Paramètres": params}
        with pd.ExcelWriter(buf, engine="openpyxl") as xl:
            for nom, df in feuilles.items():
                (df if not df.empty else pd.DataFrame({"Info": ["Aucune donnée"]})).to_excel(xl, sheet_name=nom, index=False)
                ws = xl.sheets[nom]
                for col in ws.columns:
                    largeur = max(len(str(c.value)) if c.value is not None else 0 for c in col[:200])
                    ws.column_dimensions[col[0].column_letter].width = min(max(10, largeur + 2), 45)
            self._feuille_presentation(xl.book)
        return buf.getvalue()

    def _feuille_presentation(self, book) -> None:
        """Première feuille du classeur : logo, nom de l'entreprise et chiffres clés."""
        from openpyxl.drawing.image import Image as XlImage
        from openpyxl.styles import Font
        ws = book.create_sheet(self.p.entreprise, 0)
        ws.sheet_view.showGridLines = False
        logo = Path(__file__).resolve().parent / "assets" / "logo_banniere.png"
        if logo.exists():
            img = XlImage(str(logo))
            img.width = img.height = 150
            ws.add_image(img, "A1")
        ws["C2"] = self.p.entreprise
        ws["C2"].font = Font(size=26, bold=True, color="0A2A5E")
        ws["C3"] = "Simulation de livraison e-commerce en Île-de-France"
        ws["C3"].font = Font(size=13, color="F7780F", bold=True)
        ws["C4"] = self.p.entrepot_nom
        bilan = self.bilan_df()
        if not bilan.empty:
            lignes = [("Jours simulés", len(bilan)), ("Colis livrés", int(bilan["Colis livrés"].sum())),
                      ("Colis restants", int(bilan["Colis restants"].iloc[-1])),
                      ("Distance totale (km)", round(float(bilan["Distance (km)"].sum()), 1)),
                      ("Recettes totales (€)", round(float(bilan["Total des recettes"].sum()), 2)),
                      ("Coûts totaux (€)", round(float(bilan["Total des coûts"].sum()), 2)),
                      ("Résultat net (€)", round(float(bilan["Résultat net"].sum()), 2))]
            for i, (nom, val) in enumerate(lignes, start=11):
                ws.cell(row=i, column=1, value=nom).font = Font(bold=True)
                ws.cell(row=i, column=2, value=val)
        ws.column_dimensions["A"].width = 26
        ws.column_dimensions["B"].width = 16
        ws.column_dimensions["C"].width = 60
