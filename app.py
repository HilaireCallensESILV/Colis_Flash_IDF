"""Interface Streamlit.  Lancement :  streamlit run app.py"""
import base64
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from PIL import Image

from engine import SECTEURS, Simulation, hhmm
from models import JOURS

ROOT = Path(__file__).resolve().parent
ENTREPRISE = "ColisFlash IDF"
LOGO = ROOT / "assets" / "logo.png"
LOGO_BANNIERE = ROOT / "assets" / "logo_banniere.png"
ICONE = ROOT / "assets" / "icone.png"
MENU = ["Colis livrés", "Colis restants", "Conducteurs", "Tableau de bord", "Carte des tournées",
        "Journée suivante", "Arrêter"]
COULEURS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.set_page_config(page_title=f"{ENTREPRISE} — Simulation de livraison", layout="wide",
                   page_icon=Image.open(ICONE) if ICONE.exists() else "🚚")
st.markdown("""
<style>
.block-container {padding-top: 1.5rem;}
.banner {display: flex; align-items: center; gap: 1.1rem; color: white; padding: .9rem 1.4rem; border-radius: 12px;
         margin-bottom: 1rem; background: linear-gradient(90deg,#0A2A5E 0%,#123f86 65%,#F7780F 135%);
         border-bottom: 4px solid #F7780F;}
.banner .logo {background: white; border-radius: 12px; padding: 4px; line-height: 0;}
.banner .logo img {height: 74px; width: 74px;}
.banner h2 {margin: 0; color: white;} .banner p {margin: .2rem 0 0 0; opacity: .9;}
.banner .marque {font-weight: 700; letter-spacing: .04em; color: #FFB066;}
div[data-testid="stMetric"] {background: #f4f7fa; border-radius: 10px; padding: .6rem .9rem; border: 1px solid #e1e8ef;}
@media (prefers-color-scheme: dark) {div[data-testid="stMetric"] {background: #1e2630; border-color: #2c3745;}}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- état
def nouvelle_simulation():
    st.session_state.sim = Simulation(data_dir=ROOT / "data")
    st.session_state.page = "Journée suivante"
    for cle in ("in_g", "in_m", "in_p", "in_gc", "in_pc", "in_nd", "in_auto"):
        st.session_state.pop(cle, None)


if "sim" not in st.session_state:
    nouvelle_simulation()
if "goto" in st.session_state:                      # changement de page demandé par un bouton
    st.session_state.page = st.session_state.pop("goto")

sim = st.session_state.sim
rapport = sim.dernier_rapport
eur = lambda v: f"{v:,.0f} €".replace(",", " ")

# ---------------------------------------------------------------- barre latérale
with st.sidebar:
    if LOGO.exists():
        st.image(str(LOGO), width="stretch")
    st.title(ENTREPRISE)
    st.caption(sim.p.entrepot_nom)
    st.markdown(f"**{sim.libelle_jour}**")
    st.radio("Menu", MENU, key="page")
    st.caption(f"Flotte : {sim.n_grands} grand(s) + {sim.n_petits} petit(s) camion(s) · {len(sim.conducteurs)} conducteurs")
    st.caption(f"Mois = {sim.p.jours_par_mois} jours · jour du mois : {sim.jour_du_mois + 1}")
    if not sim.pool.reelles:
        st.warning("Adresses : points GPS aléatoires. Lance `python build_address_pool.py` pour utiliser de vraies adresses.")

page = st.session_state.page
def _logo_b64() -> str:
    return base64.b64encode(LOGO_BANNIERE.read_bytes()).decode() if LOGO_BANNIERE.exists() else ""


st.markdown(
    f"<div class='banner'><div class='logo'><img src='data:image/png;base64,{_logo_b64()}' alt='{ENTREPRISE}'></div>"
    f"<div><div class='marque'>{ENTREPRISE.upper()}</div><h2>{page}</h2><p>{sim.libelle_jour}</p></div></div>",
    unsafe_allow_html=True)


def kpis():
    if rapport is None:
        return
    f = rapport.finance
    c = st.columns(5)
    c[0].metric("Nouveaux colis", rapport.nouveaux_colis)
    c[1].metric("Colis livrés", rapport.livres)
    c[2].metric("Colis restants", rapport.restants)
    c[3].metric("Camions / tournées", f"{len({r.camion_id for r in rapport.routes})} / {len(rapport.routes)}")
    c[4].metric("Résultat net du jour", eur(f["Résultat net"]))
    if rapport.secteurs != "Tous":
        st.caption(f"Secteurs livrés ce jour : {rapport.secteurs}. Les clients prioritaires sont livrés quel que soit leur secteur.")
    if rapport.pannes:
        st.warning("Camion(s) en panne ce matin : " + ", ".join(rapport.pannes))
    if rapport.moteur.startswith("heuristique"):
        st.info("Solveur de secours utilisé : OR-Tools est bloqué ou indisponible sur cet ordinateur. Les tournées sont "
                "calculées par une heuristique (insertion + 2-opt), un peu moins optimisée mais qui respecte toutes les règles.")
    if rapport.mode_trajet != "OSRM":
        st.warning("Trajets estimés à vol d'oiseau × 1,3 (OSRM désactivé ou injoignable)."
                   + (f" Détail : {rapport.erreur_trajet[:120]}" if rapport.erreur_trajet else ""))


def besoin_journee():
    st.info("Aucune journée simulée pour le moment. Va dans « Journée suivante » pour lancer la première.")


def bouton_excel(cle):
    st.download_button("Télécharger le bilan Excel", sim.exporter_excel(), "bilan_colisflash_idf.xlsx", mime=XLSX, key=cle)


# ---------------------------------------------------------------- pages
if page == "Colis livrés":
    if rapport is None:
        besoin_journee()
    else:
        kpis()
        portee = st.radio("Afficher", ["Journée en cours", "Toutes les journées"], horizontal=True)
        df = pd.DataFrame(sim.livraisons)
        if portee == "Journée en cours" and not df.empty:
            df = df[df["Jour"] == sim.jour + 1]
        if df.empty:
            st.write("Aucun colis livré.")
        else:
            st.dataframe(df, width="stretch", hide_index=True)
            st.download_button("Télécharger en CSV", df.to_csv(index=False).encode("utf-8"), "colis_livres.csv")

elif page == "Colis restants":
    if rapport is None:
        besoin_journee()
    else:
        df = sim.colis_restants_df()
        st.metric("Colis en attente de livraison", len(df))
        st.caption("Triés par ordre de priorité : prioritaires, puis les plus anciens, puis ceux du jour.")
        if not df.empty:
            st.dataframe(df, width="stretch", hide_index=True)

elif page == "Conducteurs":
    if rapport is None:
        besoin_journee()
    else:
        rows = []
        for d in sim.conducteurs:
            statut = rapport.statuts.get(d.id, "—") if sim.journee_faite else "—"
            retour = rapport.retours.get(d.id)
            rows.append({
                "Conducteur": f"{d.id} — {d.prenom} {d.nom}",
                "A travaillé aujourd'hui": "Oui" if statut == "Travail" else "Non",
                "Statut du jour": statut,
                "Colis livrés": rapport.colis_par_conducteur.get(d.id, 0),
                "Retour à l'entrepôt": hhmm(retour) if retour else "—",
                f"Congés payés pris (min {sim.p.conges_min_mois})": f"{d.conges_payes_pris} / {sim.p.conges_min_mois}",
                "Congés offerts": d.conges_offerts,
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        st.subheader("Planning de la semaine")
        plan = {f"{d.id} — {d.prenom}": dict(sim.planning_semaine(d)) for d in sim.conducteurs}
        planning = pd.DataFrame(plan).T[JOURS]

        def couleur(v):
            if v == "Travail":
                return "background-color:#c8e6c9;color:#1b5e20"
            if str(v).startswith("Congé"):
                return "background-color:#ffe0b2;color:#e65100"
            if v == "Repos hebdo":
                return "background-color:#e0e0e0;color:#424242"
            return "color:#6b7280"

        st.dataframe(planning.style.map(couleur), width="stretch")
        st.caption("« Disponible » = jour de présence : le conducteur travaillera ou sera en congé selon le nombre de colis. "
                   "Ses 3 congés payés minimum doivent être pris avant la fin du mois.")

elif page == "Tableau de bord":
    if rapport is None:
        besoin_journee()
    else:
        bilan = sim.bilan_df()
        jours = list(bilan["Jour"])
        choix = st.selectbox("Journée affichée", jours, index=len(jours) - 1)
        b = bilan[bilan["Jour"] == choix].iloc[0]
        st.subheader("Activité")
        c = st.columns(5)
        c[0].metric("Taux de service", f"{b['Taux de service (%)']} %", help="Colis livrés / colis à livrer ce jour-là")
        c[1].metric("Prioritaires livrés", f"{b['Prioritaires livrés (%)']} %")
        c[2].metric("Remplissage des camions", f"{b['Remplissage des camions (%)']} %")
        c[3].metric("Utilisation des conducteurs", f"{b['Utilisation des conducteurs (%)']} %", help="Temps actif / 7 h")
        c[4].metric("Km par colis", b["Km par colis"])
        c = st.columns(6)
        c[5].metric("Secteurs du jour", b["Secteurs du jour"])
        c[0].metric("Distance", f"{b['Distance (km)']} km")
        c[1].metric("Tournées (rotations)", int(b["Tournées (rotations)"]))
        c[2].metric("Âge moyen des restants", f"{b['Âge moyen des restants (jours)']} j")
        c[3].metric("Pannes", int(b["Pannes"]))
        c[4].metric("Trajets", b["Trajets"])

        st.subheader("Finances du jour")
        c = st.columns(4)
        c[0].metric("Recettes", eur(b["Total des recettes"]),
                    help=(f"Marge par taille de colis : {eur(b['Marge sur colis livrés'])} + supplément express : {eur(b['Supplément express'])}"
                          f" + supplément zone éloignée : {eur(b['Supplément zone éloignée'])}"))
        c[1].metric("Total des coûts", eur(b["Total des coûts"]))
        c[2].metric("Résultat net", eur(b["Résultat net"]))
        c[3].metric("Coût par colis livré", f"{b['Coût par colis livré']:.2f} €" if pd.notna(b["Coût par colis livré"]) else "—")
        postes = ["Salaires", "Entrepôt : employé", "Entrepôt : location, électricité, charges", "Camions (coût journalier)",
                  "Entretien préventif des camions", "Carburant",
                  "Réparations (pannes)", "Stockage", "Pénalités de retard"]
        st.bar_chart(pd.DataFrame({"Coût (€)": [b[p] for p in postes]}, index=postes), horizontal=True)

        st.subheader("Évolution sur toute la simulation")
        evo = bilan.set_index("Jour")
        c1, c2 = st.columns(2)
        c1.caption("Résultat net cumulé (€)")
        c1.line_chart(evo[["Résultat net"]].cumsum())
        c2.caption("Colis livrés et restants par jour")
        c2.line_chart(evo[["Colis livrés", "Colis restants"]])
        c1, c2 = st.columns(2)
        c1.caption("Répartition cumulée des coûts (€)")
        c1.bar_chart(evo[postes].sum().to_frame("Coût cumulé (€)"), horizontal=True)
        c2.caption("Kilomètres parcourus par jour")
        c2.bar_chart(evo[["Distance (km)"]])
        total = evo["Résultat net"].sum()
        st.info(f"Résultat net cumulé : **{eur(total)}** sur {len(evo)} jour(s).")
        with st.expander("Tableau complet par jour"):
            st.dataframe(bilan, width="stretch", hide_index=True)
        bouton_excel("xl_dashboard")

elif page == "Carte des tournées":
    if rapport is None:
        besoin_journee()
    else:
        kpis()
        with st.spinner("Récupération des tracés routiers (OSRM)…"):
            sim.charger_traces(rapport)
        m = folium.Map(location=[sim.p.entrepot_lat, sim.p.entrepot_lon], zoom_start=10, tiles="OpenStreetMap")
        folium.Marker([sim.p.entrepot_lat, sim.p.entrepot_lon], tooltip=sim.p.entrepot_nom,
                      icon=folium.Icon(color="black", icon="home")).add_to(m)
        camions = {c.id: c for c in sim.camions}
        ordre = list(dict.fromkeys(r.camion_id for r in rapport.routes))
        for route in rapport.routes:
            couleur_r = COULEURS[ordre.index(route.camion_id) % len(COULEURS)]
            camion = camions.get(route.camion_id)
            for arret in route.arrets:
                cl = sim.clients[arret["client_id"]]
                folium.CircleMarker([cl.lat, cl.lon], radius=5, color=couleur_r, fill=True, fill_opacity=.9,
                                    tooltip=(f"{route.camion_id} · tournée {route.rotation} · arrêt {arret['ordre']} · {arret['arrivee']}<br>"
                                             f"{cl.prenom} {cl.nom} · {cl.secteur} · {arret['nb_colis']} colis"
                                             f"{' · PRIORITAIRE' if cl.prioritaire else ''}")).add_to(m)
            folium.PolyLine(route.geometrie, color=couleur_r, weight=4, opacity=.8,
                            dash_array=None if route.rotation == 1 else "8 6",
                            tooltip=(f"{route.camion_id} ({camion.taille if camion else ''}, {camion.plaque if camion else ''}) · "
                                     f"tournée {route.rotation} · {route.distance_km} km · charge {route.charge}/{route.capacite} · "
                                     f"{hhmm(route.depart_min)} → {hhmm(route.retour_min)}")).add_to(m)
        for cl in sim.clients.values():
            if cl.colis_restants:
                folium.CircleMarker([cl.lat, cl.lon], radius=3, color="#888", fill=True, fill_opacity=.6,
                                    tooltip=f"En attente : {cl.id}").add_to(m)
        html_carte = m._repr_html_()
        if hasattr(st, "iframe"):                       # Streamlit récent
            st.iframe(html_carte, height=620)
        else:                                           # anciennes versions
            components.html(html_carte, height=620)
        if rapport.routes and not all(r.geometrie_osrm for r in rapport.routes):
            st.warning("Le tracé routier OSRM n'a pas pu être récupéré pour certaines tournées : traits droits affichés.")
        else:
            st.caption("Tracé routier réel (OSRM). Une couleur par camion, tirets = 2e tournée ou plus. Gris : colis restants.")
        if rapport.routes:
            st.dataframe(sim.tournees_df().query("Jour == @sim.jour + 1"), width="stretch", hide_index=True)

elif page == "Journée suivante":
    cible = sim.jour + 1 if sim.journee_faite else sim.jour
    st.subheader(f"Jour {cible + 1} — {JOURS[cible % 7]}")
    if sim.p.livraison_par_secteur:
        i = cible % len(SECTEURS)
        du_jour = " + ".join(SECTEURS[(i + k) % len(SECTEURS)] for k in range(sim.p.secteurs_par_jour))
        st.info(f"Livraison par secteur : ce jour, secteurs **{du_jour}**. Les clients des autres secteurs attendent leur tour "
                "(1 jour sur 2), sauf les clients prioritaires, livrés tous les jours.")
    st.write("**Colis commandés pour cette journée**")
    c1, c2, c3 = st.columns(3)
    n_grand = c1.number_input("Grands colis (volume 5)", min_value=0, max_value=500, value=10, key="in_g")
    n_moyen = c2.number_input("Moyens colis (volume 3)", min_value=0, max_value=500, value=15, key="in_m")
    n_petit = c3.number_input("Petits colis (volume 1)", min_value=0, max_value=500, value=20, key="in_p")
    st.write("**Ressources de la journée**")
    volume_new = 5 * int(n_grand) + 3 * int(n_moyen) + int(n_petit)
    rec_g, rec_p, rec_c = sim.flotte_recommandee(int(n_grand + n_moyen + n_petit), volume_new)
    auto = st.checkbox("Flotte automatique : n'utiliser que les camions et conducteurs utiles au volume du jour", key="in_auto")
    st.caption(f"Recommandation pour {int(n_grand + n_moyen + n_petit)} nouveaux colis + les colis en attente : "
               f"{rec_g} grand(s) camion(s), {rec_p} petit(s) camion(s), {rec_c} conducteur(s).")
    if auto:
        n_gc, n_pc, n_nd = rec_g, rec_p, rec_c
        st.success(f"Flotte automatique : {n_gc} grand(s) + {n_pc} petit(s) camion(s) et {n_nd} conducteur(s).")
    else:
        def appliquer(g, pt, c):
            st.session_state.in_gc, st.session_state.in_pc, st.session_state.in_nd = g, pt, c
        c1, c2, c3 = st.columns(3)
        n_gc = c1.number_input(f"Grands camions ({sim.p.capacite_grand} volumes)", min_value=0, max_value=30, value=sim.n_grands, key="in_gc")
        n_pc = c2.number_input(f"Petits camions ({sim.p.capacite_petit} volumes)", min_value=0, max_value=30, value=sim.n_petits, key="in_pc")
        n_nd = c3.number_input("Conducteurs employés", min_value=1, max_value=30, value=len(sim.conducteurs), key="in_nd")
        st.button("Appliquer la recommandation", on_click=appliquer, args=(rec_g, rec_p, rec_c))
    p = sim.p
    fixes = (n_nd * p.cout_conducteur_jour + p.cout_entrepot_jour + n_gc * p.cout_camion("grand") + n_pc * p.cout_camion("petit")
             + (n_gc + n_pc) * p.cout_entretien_camion_jour)
    st.caption(f"Coûts fixes du jour : {n_nd} conducteurs × {p.cout_conducteur_jour:.2f} € "
               f"+ entrepôt {p.cout_entrepot_jour:.0f} € (employé {p.cout_employe_entrepot_jour:.2f} € + location et électricité "
               f"{p.cout_location_entrepot_jour:.2f} €) "
               f"+ camions {n_gc * p.cout_camion('grand') + n_pc * p.cout_camion('petit'):.0f} € + entretien "
               f"{(n_gc + n_pc) * p.cout_entretien_camion_jour:.0f} € = **{fixes:,.0f} €**".replace(",", " "))
    st.info("Les autres informations relatives aux conducteurs, clients, camions sont choisies de manière "
            "aléatoire ou définies par défaut.")
    st.caption("Le calcul des tournées prend quelques secondes à environ une minute (calcul des trajets OSRM).")
    if n_gc + n_pc == 0:
        st.error("Il faut au moins un camion.")
    if st.button("Lancer la journée", type="primary", disabled=(n_gc + n_pc == 0)):
        with st.spinner("Calcul des trajets et optimisation des tournées en cours…"):
            if sim.journee_faite:
                sim.jour_suivant()
            sim.ajuster_ressources(int(n_gc), int(n_pc), int(n_nd))
            sim.run_day(int(n_grand), int(n_moyen), int(n_petit))
        st.session_state.goto = "Colis livrés"
        st.rerun()

elif page == "Arrêter":
    st.subheader("Simulation terminée")
    if rapport is None:
        st.write("Aucune journée n'a été simulée.")
    else:
        bilan = sim.bilan_df()
        c = st.columns(5)
        c[0].metric("Jours simulés", len(sim.rapports))
        c[1].metric("Colis livrés", len(sim.livraisons))
        c[2].metric("Colis restants", rapport.restants)
        c[3].metric("Distance totale", f"{bilan['Distance (km)'].sum():.0f} km")
        c[4].metric("Résultat net cumulé", eur(bilan["Résultat net"].sum()))
        bouton_excel("xl_stop")
        if sim.livraisons:
            st.download_button("Télécharger tous les colis livrés (CSV)",
                               pd.DataFrame(sim.livraisons).to_csv(index=False).encode("utf-8"), "colis_livres.csv")
    st.button("Recommencer une nouvelle simulation", on_click=nouvelle_simulation)
    st.caption("Tu peux maintenant fermer l'onglet et arrêter le serveur avec Ctrl + C dans le terminal.")
