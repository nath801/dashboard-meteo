"""Interface Streamlit : filtres, KPI, alertes, graphiques Plotly et carte AQI."""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src import config
from src.alerts import AlertEngine, aqi_category
from src.api_client import WeatherAPIClient, fetch_all_cities
from src.auth import IPRateLimiter, UserStore, security_event
from src.captcha import Captcha
from src.data_processing import DataProcessor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
logger = logging.getLogger(__name__)

SOURCE_LABELS = {
    "api": "🟢 API Open-Meteo (temps réel)",
    "cache": f"🔵 Cache local SQLite (TTL {config.CACHE_TTL_MINUTES} min)",
    "mock": "🟠 Données simulées (mode déconnecté)",
}
COMPASS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
OTHER_CITY = "Autre ville…"


@st.cache_resource
def get_client() -> WeatherAPIClient:
    """Client API partagé entre les sessions (le cache SQLite est persistant)."""
    return WeatherAPIClient()


def local_now(raw: Dict[str, Any]) -> pd.Timestamp:
    """Heure actuelle à l'heure locale de la ville (les horodatages de l'API sont locaux)."""
    offset = int((raw.get("weather") or {}).get("utc_offset_seconds", 0) or 0)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=offset)
    return pd.Timestamp(now)


def wind_dir(deg: float) -> str:
    """Convertit un angle en point cardinal."""
    if pd.isna(deg):
        return ""
    return COMPASS[int((deg % 360) / 45 + 0.5) % 8]


def fmt(value: Any, pattern: str = "{:.1f}", unit: str = "") -> str:
    """Formate une valeur numérique (« — » si absente)."""
    if value is None or pd.isna(value):
        return "—"
    return pattern.format(value) + (f" {unit}" if unit else "")


# --------------------------------------------------------------------- Authentification
@st.cache_resource
def get_user_store() -> UserStore:
    """Magasin de comptes partagé (verrou commun entre les sessions)."""
    return UserStore()


def client_ip() -> str:
    """IP du visiteur : dernière valeur de X-Forwarded-For (posée par Caddy), sinon IP directe."""
    try:
        forwarded = st.context.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:45]
        return st.context.ip_address or "-"
    except Exception:  # contexte indisponible (tests, exécution hors Streamlit)
        return "-"


@st.cache_resource
def get_signup_limiter() -> IPRateLimiter:
    """Compteur partagé des comptes créés par IP sur une heure (anti-création en masse)."""
    return IPRateLimiter(max_failures=config.SIGNUP_MAX_PER_HOUR, window_minutes=60)


def _captcha(key: str) -> Captcha:
    """Captcha courant de la session pour ce formulaire (recréé s'il a été utilisé ou a expiré)."""
    current = st.session_state.get(key)
    if current is None or current.used or time.time() > current.expires_at:
        current = st.session_state[key] = Captcha.new()
        st.session_state.pop(f"{key}_answer", None)  # vide la saisie de l'ancienne image
    return current


def _captcha_field(key: str) -> str:
    """Affiche l'image du captcha et le champ de saisie (à appeler dans un st.form)."""
    st.image(_captcha(key).image, width=220)
    return st.text_input("Recopiez les caractères de l'image", max_chars=8, autocomplete="off",
                         key=f"{key}_answer", help="Majuscules ou minuscules, sans importance.")


def _check_captcha(key: str, answer: str, form: str) -> bool:
    """Vérifie le captcha (usage unique) et journalise l'échec."""
    if _captcha(key).verify(answer):
        return True
    security_event("CAPTCHA_FAILED", "-", client_ip(), form=form)
    return False


def _flash(form: str, kind: str, message: str) -> None:
    """Mémorise un message à afficher dans l'onglet ``form`` après le rechargement de la page."""
    st.session_state[f"flash_{form}"] = (kind, message)
    st.rerun()  # nouvelle image de captcha + message


def _show_flash(form: str) -> None:
    flash = st.session_state.pop(f"flash_{form}", None)
    if flash:
        getattr(st, flash[0])(flash[1])


def login_page() -> None:
    """Page de connexion, et de création de compte si elle est autorisée (ALLOW_SIGNUP)."""
    store = get_user_store()
    ip = client_ip()
    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        st.markdown("<h2 style='text-align:center;white-space:nowrap'>🌤️ Dashboard Météo</h2>",
                    unsafe_allow_html=True)  # texte fixe
        st.markdown("<p style='text-align:center;color:#777'>Connectez-vous pour accéder au tableau de bord</p>",
                    unsafe_allow_html=True)  # texte fixe
        names = ["🔐 Connexion"] + (["📝 Créer un compte"] if config.ALLOW_SIGNUP else [])
        tabs = st.tabs(names)

        # Captcha à la connexion après plusieurs échecs (dans la session ou depuis cette IP).
        need_captcha = max(st.session_state.get("login_failures", 0),
                           store.ip_limiter.count(ip)) >= config.CAPTCHA_AFTER_FAILURES
        with tabs[0]:
            _show_flash("login")
        with tabs[0], st.form("login"):
            username = st.text_input("Identifiant", max_chars=32, autocomplete="username")
            password = st.text_input("Mot de passe", type="password", max_chars=128,
                                     autocomplete="current-password")
            answer = _captcha_field("captcha_login") if need_captcha else ""
            if st.form_submit_button("Se connecter", type="primary", width="stretch"):
                if need_captcha and not _check_captcha("captcha_login", answer, "login"):
                    _flash("login", "error", "Captcha incorrect ou expiré : recopiez la nouvelle image.")
                result = store.authenticate(username, password, ip)
                if result.ok:
                    st.session_state.pop("login_failures", None)
                    st.session_state["user"] = result.username
                    st.session_state["last_activity"] = time.time()
                    st.rerun()
                st.session_state["login_failures"] = st.session_state.get("login_failures", 0) + 1
                _flash("login", "error", result.message)

        if config.ALLOW_SIGNUP:
            with tabs[1]:
                _show_flash("signup")
                with st.form("signup", clear_on_submit=False):
                    new_user = st.text_input("Identifiant", max_chars=32, autocomplete="username",
                                             key="signup_user")
                    pwd1 = st.text_input("Mot de passe", type="password", max_chars=128,
                                         autocomplete="new-password", key="signup_pwd1")
                    pwd2 = st.text_input("Confirmer le mot de passe", type="password", max_chars=128,
                                         autocomplete="new-password", key="signup_pwd2")
                    st.caption("3 à 32 caractères (lettres, chiffres, . _ -) pour l'identifiant. "
                               "Mot de passe : 8 caractères minimum, au moins une lettre et un chiffre, "
                               "pas de mot de passe courant.")
                    answer = _captcha_field("captcha_signup")
                    if st.form_submit_button("Créer le compte", width="stretch"):
                        limiter = get_signup_limiter()
                        if limiter.is_blocked(ip):
                            security_event("SIGNUP_BLOCKED_IP", new_user, ip)
                            _flash("signup", "error", "Trop de comptes créés depuis cette adresse. Réessayez plus tard.")
                        if not _check_captcha("captcha_signup", answer, "signup"):
                            _flash("signup", "error", "Captcha incorrect ou expiré : recopiez la nouvelle image.")
                        if pwd1 != pwd2:
                            _flash("signup", "error", "Les mots de passe ne correspondent pas.")
                        try:
                            store.add_user(new_user, pwd1, ip)
                        except ValueError as exc:
                            _flash("signup", "error", str(exc))
                        limiter.record_failure(ip)  # compte une création pour cette IP
                        _flash("signup", "success",
                               f"Compte « {new_user.strip().lower()} » créé : connectez-vous dans l'onglet Connexion.")
                if st.button("🔄 Autre image", key="captcha_refresh"):
                    st.session_state.pop("captcha_signup", None)
                    st.rerun()


def logout(reason: str = "") -> None:
    """Termine la session de l'utilisateur et journalise l'événement."""
    security_event("SESSION_EXPIRED" if reason else "LOGOUT", st.session_state.get("user", "-"), client_ip())
    for key in ("user", "last_activity"):
        st.session_state.pop(key, None)
    if reason:
        st.session_state["logout_reason"] = reason


def session_is_valid() -> bool:
    """Vérifie la connexion et l'expiration après inactivité (met à jour l'horodatage)."""
    if not st.session_state.get("user"):
        return False
    idle = time.time() - st.session_state.get("last_activity", 0)
    if idle > config.SESSION_TIMEOUT_MINUTES * 60:
        logout("Session expirée après inactivité : reconnectez-vous.")
        return False
    st.session_state["last_activity"] = time.time()
    return True


# --------------------------------------------------------------------- Barre latérale
def sidebar(client: WeatherAPIClient) -> Dict[str, Any]:
    """Construit le panneau de sélection et renvoie les choix de l'utilisateur."""
    sb = st.sidebar
    sb.markdown(f"👤 Connecté : **{st.session_state['user']}**")
    if sb.button("🚪 Se déconnecter"):
        logout()
        st.rerun()
    sb.divider()
    sb.header("⚙️ Configuration")
    choice = sb.selectbox("Ville", list(config.CITIES) + [OTHER_CITY])
    city: Optional[Dict[str, Any]]
    if choice == OTHER_CITY:
        query = sb.text_input("Nom de la ville", max_chars=60, placeholder="ex. Bordeaux")
        city = client.geocode(query) if query else None
        if query and city is None:
            sb.error("Ville introuvable.")
        elif city:
            sb.caption(f"📍 {city['name']} ({city['country']})")
    else:
        city = {"name": choice, "country": "", **config.CITIES[choice]}

    today = dt.date.today()
    period = sb.date_input("Période affichée", value=(today - dt.timedelta(days=7), today + dt.timedelta(days=1)),
                           min_value=today - dt.timedelta(days=7), max_value=today + dt.timedelta(days=1),
                           format="DD/MM/YYYY")
    weather_cols = sb.multiselect("Métriques météo", config.WEATHER_VARS,
                                  default=["temperature_2m", "apparent_temperature"],
                                  format_func=config.LABELS.get)
    air_cols = sb.multiselect("Polluants", list(config.THRESHOLDS),
                              default=["pm2_5", "pm10", "nitrogen_dioxide"], format_func=config.LABELS.get)
    window = sb.slider("Fenêtre des statistiques (heures)", 6, 168, 24, step=6)
    mock = sb.toggle("Mode simulé (données hors ligne)", value=False)
    if sb.button("🗑️ Vider le cache"):
        client.cache.clear()
        sb.success("Cache vidé.")
    return {"city": city, "period": period, "weather_cols": weather_cols, "air_cols": air_cols,
            "window": window, "mock": mock}


# --------------------------------------------------------------------- Graphiques
def time_chart(df: pd.DataFrame, cols: List[str], now: pd.Timestamp, title: str,
               thresholds: bool = False) -> go.Figure:
    """Courbes temporelles avec zone de prévision grisée et ligne « maintenant »."""
    fig = go.Figure()
    for c in cols:
        if c in df:
            fig.add_trace(go.Scatter(x=df.index, y=df[c], mode="lines", name=config.LABELS.get(c, c)))
            if thresholds and c in config.THRESHOLDS:
                name, warn, _, win, _ = config.THRESHOLDS[c]
                fig.add_hline(y=warn, line_dash="dot", line_color="#d35400",
                              annotation_text=f"OMS {name} ({win} h) : {warn:g}",
                              annotation_position="top left")
    if len(df) and df.index.max() > now:
        fig.add_vrect(x0=now, x1=df.index.max(), fillcolor="#9e9e9e", opacity=0.15, line_width=0,
                      annotation_text="Prévisions", annotation_position="top left")
        fig.add_vline(x=now, line_dash="dot", line_color="#555")
    fig.update_layout(title=title, height=420, margin=dict(l=10, r=10, t=50, b=10),
                      hovermode="x unified", xaxis_title=None, yaxis_title="Valeur",
                      legend=dict(orientation="h", y=-0.15))
    return fig


def render_kpis(past: pd.DataFrame, now: pd.Timestamp) -> None:
    """Affiche les 12 indicateurs clés sur deux lignes + le badge de catégorie AQI."""
    last = DataProcessor.latest_valid(past)
    prev = DataProcessor.latest_valid(past[past.index <= now - pd.Timedelta(hours=24)])
    today = past[past.index.date == now.date()]

    def delta(col: str, unit: str, pattern: str = "{:+.1f}") -> Optional[str]:
        if col in last and col in prev and pd.notna(last[col]) and pd.notna(prev[col]):
            return pattern.format(last[col] - prev[col]) + f" {unit} /24h"
        return None

    r1 = st.columns(6)
    r1[0].metric("🌡️ Température", fmt(last.get("temperature_2m"), unit="°C"), delta("temperature_2m", "°C"))
    r1[1].metric("🥶 Ressentie", fmt(last.get("apparent_temperature"), unit="°C"))
    tmin, tmax = (today["temperature_2m"].min(), today["temperature_2m"].max()) if len(today) else (None, None)
    r1[2].metric("↕️ Min / Max du jour", f"{fmt(tmin)} / {fmt(tmax, unit='°C')}")
    r1[3].metric("💧 Humidité", fmt(last.get("relative_humidity_2m"), "{:.0f}", "%"))
    r1[4].metric("🧭 Pression", fmt(last.get("pressure_msl"), "{:.0f}", "hPa"))
    r1[5].metric("🌬️ Vent", f"{fmt(last.get('wind_speed_10m'), '{:.0f}', 'km/h')} "
                 f"{wind_dir(last.get('wind_direction_10m', float('nan')))}")

    r2 = st.columns(6)
    r2[0].metric("☁️ Nuages", fmt(last.get("cloud_cover"), "{:.0f}", "%"))
    r2[1].metric("🌧️ Précipitations", fmt(last.get("precipitation"), unit="mm"))
    r2[2].metric("🏭 AQI (US)", fmt(last.get("us_aqi"), "{:.0f}"),
                 delta("us_aqi", "", "{:+.0f}"), delta_color="inverse")
    r2[3].metric("PM2.5 (µg/m³)", fmt(last.get("pm2_5")))
    r2[4].metric("NO2 (µg/m³)", fmt(last.get("nitrogen_dioxide")))
    r2[5].metric("❤️ Score confort", fmt(last.get("comfort_score"), unit="/100"))

    label, color = aqi_category(last.get("us_aqi"))
    st.markdown(
        f"Qualité de l'air : <span style='background:{color};padding:3px 10px;border-radius:6px;"
        f"font-weight:600;color:{'#000' if label in ('Bon', 'Modéré') else '#fff'}'>{label}</span>",
        unsafe_allow_html=True,  # contenu 100 % interne (libellé et couleur de config.AQI_SCALE)
    )


def render_map(client: WeatherAPIClient, mock: bool) -> None:
    """Carte OpenStreetMap des villes colorées par catégorie AQI."""
    rows = []
    for name, raw in fetch_all_cities(client, config.CITIES, mock).items():
        past, _ = DataProcessor.split_past_future(DataProcessor.to_dataframe(raw), local_now(raw))
        last = DataProcessor.latest_valid(past, ["us_aqi", "temperature_2m", "pm2_5"])
        label, _ = aqi_category(last.get("us_aqi"))
        rows.append({"Ville": name, "lat": config.CITIES[name]["lat"], "lon": config.CITIES[name]["lon"],
                     "AQI US": last.get("us_aqi"), "Catégorie": label,
                     "T° (°C)": last.get("temperature_2m"), "PM2.5": last.get("pm2_5")})
    mdf = pd.DataFrame(rows)
    colors = {lbl: col for _, lbl, col in config.AQI_SCALE} | {"Inconnu": "#bdbdbd"}
    fig = px.scatter_map(mdf, lat="lat", lon="lon", color="Catégorie", color_discrete_map=colors,
                         hover_name="Ville", hover_data={"AQI US": True, "T° (°C)": True, "PM2.5": True,
                                                         "lat": False, "lon": False},
                         size=[22] * len(mdf), size_max=22, zoom=4.2, center={"lat": 46.5, "lon": 2.0},
                         map_style="open-street-map", height=560)
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0))
    st.plotly_chart(fig, width="stretch")
    st.dataframe(mdf.drop(columns=["lat", "lon"]), hide_index=True, width="stretch")


# --------------------------------------------------------------------- Page principale
def render_dashboard() -> None:
    """Page principale du dashboard."""
    client = get_client()
    opts = sidebar(client)
    st.title("🌤️ Dashboard Météo & Qualité de l'Air")
    city = opts["city"]
    if not city:
        st.info("Saisissez une ville dans le panneau de gauche.")
        return

    with st.spinner(f"Chargement des données pour {city['name']}…"):
        raw = client.fetch(city["lat"], city["lon"], mock_mode=opts["mock"])
        df = DataProcessor.to_dataframe(raw)
    if df.empty:
        st.error("Impossible de récupérer les données.")
        return

    now = local_now(raw)
    past, _ = DataProcessor.split_past_future(df, now)
    st.caption(f"**{city['name']}** · {city['lat']:.3f}, {city['lon']:.3f} · "
               f"Heure locale : {now:%d/%m/%Y %H:%M} · Source : {SOURCE_LABELS[client.last_source]}")
    if client.last_source == "mock" and not opts["mock"]:
        st.warning("API injoignable : affichage de données simulées.")

    alerts = AlertEngine.evaluate(past)
    for a in alerts:
        (st.error if a.level == "danger" else st.warning)(a.message, icon="🚨" if a.level == "danger" else "⚠️")
    if not alerts:
        st.success("Aucun dépassement des seuils OMS.", icon="✅")

    render_kpis(past, now)
    st.divider()

    period = opts["period"]
    view = df
    if isinstance(period, (tuple, list)) and len(period) == 2:
        view = df[(df.index.date >= period[0]) & (df.index.date <= period[1])]

    t_meteo, t_air, t_stats, t_map = st.tabs(
        ["📈 Météo", "🏭 Qualité de l'air", "📊 Statistiques & tendances", "🗺️ Carte"])
    with t_meteo:
        st.plotly_chart(time_chart(view, opts["weather_cols"], now, "Évolution des métriques météo"),
                        width="stretch")
        st.plotly_chart(time_chart(view, ["comfort_score"], now, "Score Santé/Confort (0-100)"),
                        width="stretch")
        st.caption("Score = 100 − (|T° ressentie − 21|×2 + |humidité − 50|×0,4 + PM2.5/15×15 + NO2/25×10)")
    with t_air:
        st.plotly_chart(time_chart(view, opts["air_cols"], now, "Polluants (µg/m³) et seuils OMS",
                                   thresholds=True), width="stretch")
        st.plotly_chart(time_chart(view, ["us_aqi", "european_aqi"], now, "Indices de qualité de l'air"),
                        width="stretch")
    with t_stats:
        cols = opts["weather_cols"] + opts["air_cols"] + ["us_aqi", "comfort_score"]
        st.subheader(f"Statistiques sur les {opts['window']} dernières heures")
        st.dataframe(DataProcessor.stats(past, cols, opts["window"]), width="stretch")
        st.subheader("Tendances 24 h et 7 jours")
        st.dataframe(DataProcessor.trends_table(past, cols), hide_index=True, width="stretch")
        st.subheader("Synthèse journalière")
        st.dataframe(DataProcessor.daily_summary(past), width="stretch")
    with t_map:
        render_map(client, opts["mock"])


def main() -> None:
    """Point d'entrée de l'application Streamlit."""
    st.set_page_config(page_title="Dashboard Météo & Qualité de l'Air", page_icon="🌤️", layout="wide")
    if not session_is_valid():
        reason = st.session_state.pop("logout_reason", None)
        if reason:
            st.info(reason)
        login_page()
        return
    render_dashboard()
