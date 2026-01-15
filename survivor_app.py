import streamlit as st
import pandas as pd
import gspread
import requests
import time
import hashlib
import re
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Survivor Pool", layout="wide")

# --- 2. CONSTANTS & MAPS ---
CONFERENCE_MAP = {
    "All Division I": 50,
    "NCAA Tournament": 100,  
    "NIT Tournament": 50,    
    "ACC": 2,
    "Big 12": 4,
    "Big East": 23,
    "Big Ten": 7,
    "SEC": 8,
    "Pac-12": 9,
    "Atlantic 10": 1,
    "American": 62,
    "WCC": 29,
    "Mountain West": 18
}

# --- 3. SECURITY FUNCTIONS ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def check_hashes(password, hashed_text):
    if make_hashes(password) == hashed_text:
        return True
    return False

# --- 4. GOOGLE SHEETS FUNCTIONS ---
@st.cache_resource
def get_google_spreadsheet():
    try:
        gc = gspread.service_account(filename='service_account.json')
    except FileNotFoundError:
        gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
    return gc.open("Survivor_Test")

def check_sheet_exists(sheet_name):
    sh = get_google_spreadsheet()
    try:
        sh.worksheet(sheet_name)
        return True
    except:
        return False

def ensure_config_sheet(base_name):
    if base_name == "NCAA":
        config_name = f"Config_{base_name}"
        sh = get_google_spreadsheet()
        try:
            sh.worksheet(config_name)
        except:
            ws = sh.add_worksheet(title=config_name, rows=50, cols=3)
            ws.update([["Round_Name", "Date_String", "Group_ID"]], 'A1')

def get_round_config(base_name):
    if base_name != "NCAA": return {}
    config_name = f"Config_{base_name}"
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(config_name)
        records = ws.get_all_values()
        config = {}
        for row in records:
            if len(row) > 1 and row[0] != "Round_Name":
                r_name = row[0]
                r_date = row[1]
                r_group = int(row[2]) if len(row) > 2 and row[2].isdigit() else 50
                config[r_name] = {'date': r_date, 'group': r_group}
        return config
    except:
        return {}

def set_round_config(base_name, round_name, date_str, group_id):
    if base_name != "NCAA": return
    config_name = f"Config_{base_name}"
    sh = get_google_spreadsheet()
    ws = sh.worksheet(config_name)
    
    cell = None
    try: cell = ws.find(round_name)
    except: pass

    if cell:
        ws.update_cell(cell.row, 2, date_str)
        ws.update_cell(cell.row, 3, str(group_id))
    else:
        ws.append_row([round_name, date_str, str(group_id)])

@st.cache_data(ttl=5)
def load_data(sheet_name):
    """
    Robust data loader. 
    If get_all_records returns [], it means there are headers but no data rows.
    In that case, we manually fetch headers to ensure DataFrame columns exist.
    """
    sh = get_google_spreadsheet()
    try:
        worksheet = sh.worksheet(sheet_name)
        data = worksheet.get_all_records()
        
        if not data:
            headers = worksheet.row_values(1)
            return pd.DataFrame(columns=headers)
            
        return pd.DataFrame(data)
    except:
        return pd.DataFrame()

def initialize_sheet_headers(sheet_name, pool_type):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=100, cols=25)
    
    if pool_type == "NFL Survivor":
        headers = ["Name", "Email", "Security_Hash", "Status"] + [f"Week {i}" for i in range(1,19)]
    else:
        headers = ["Name", "Email", "Security_Hash", "Status"] + [f"Day {i}" for i in range(1,11)]
    
    ws.clear()
    ws.update([headers], 'A1')
    return True

def to_eastern(utc_dt):
    if utc_dt is None: return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(ZoneInfo("America/New_York"))

# --- 5. DATA FETCHER ---
@st.cache_data(ttl=60)
def get_sports_data(base_url, pool_type, param, group_id=50):
    fetch_time = datetime.now().strftime("%I:%M:%S %p")
    target_url = base_url
    if pool_type == "NFL Survivor":
        target_url = f"{base_url}?week={param}&seasontype=2"
    elif pool_type == "March Madness (NCAA)":
        date_str = param if param else datetime.now().strftime("%Y%m%d")
        target_url = f"{base_url}?dates={date_str}&groups={group_id}&limit=200"

    try:
        response = requests.get(target_url)
        data = response.json()
        games = []
        earliest_start = None

        if 'events' in data:
            for event in data['events']:
                short_status = event['status']['type']['description']
                state = event['status']['type']['state']
                date_str = event['date']
                try:
                    game_time = datetime.strptime(date_str, "%Y-%m-%dT%H:%MZ")
                    game_time = game_time.replace(tzinfo=timezone.utc)
                except:
                    game_time = datetime.now(timezone.utc)

                if earliest_start is None or game_time < earliest_start:
                    earliest_start = game_time

                competitors = event['competitions'][0]['competitors']
                team_0 = competitors[0]['team']['displayName']
                team_1 = competitors[1]['team']['displayName']
                score_0 = int(competitors[0].get('score', 0))
                score_1 = int(competitors[1].get('score', 0))

                winner = "TBD"
                if state == "post":
                    if score_0 > score_1: winner = team_0
                    elif score_1 > score_0: winner = team_1
                    else: winner = "Tie"
                
                is_locked_game = state in ["in", "post"]

                games.append({
                    "Team A": team_0, "Team B": team_1, "Winner": winner,
                    "Status": short_status, "Score": f"{score_0}-{score_1}",
                    "Locked": is_locked_game,
                    "StartTime": game_time
                })
        return pd.DataFrame(games), earliest_start, fetch_time
    except Exception as e:
        return pd.DataFrame(), None, fetch_time

# --- 6. USER FUNCTIONS ---
def register_user(sheet_name, name, email, password):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
        existing_data = pd.DataFrame(ws.get_all_records())
        if not existing_data.empty and 'Email' in existing_data.columns:
            if email.lower().strip() in existing_data['Email'].astype(str).str.lower().str.strip().values:
                return False, "Email already registered!"
        ws.append_row([name, email, make_hashes(password), "Alive"])
        return True, "Success"
    except Exception as e: return False, str(e)

def save_pick_to_sheet(sheet_name, player_name, week_col, team_pick):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
        cell = ws.find(player_name)
        col_idx = ws.find(week_col).col
        ws.update_cell(cell.row, col_idx, team_pick)
        return True
    except: return False

def update_player_status(sheet_name, player_name, new_status):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
        cell = ws.find(player_name)
        col_idx = ws.find("Status").col
        ws.update_cell(cell.row, col_idx, new_status)
        return True
    except: return False

# --- 7. INITIALIZE STATE & NAVIGATION ---
if 'pool_type' not in st.session_state:
    st.session_state.pool_type = "NFL Survivor" 

col_nav1, col_nav2 = st.columns(2)
with col_nav1:
    if st.button("🏈 NFL Survivor", use_container_width=True, 
                 type="primary" if st.session_state.pool_type == "NFL Survivor" else "secondary"):
        st.session_state.pool_type = "NFL Survivor"
        st.rerun()

with col_nav2:
    if st.button("🏀 March Madness (NCAA)", use_container_width=True,
                 type="primary" if st.session_state.pool_type == "March Madness (NCAA)" else "secondary"):
        st.session_state.pool_type = "March Madness (NCAA)"
        st.rerun()

st.divider()

pool_type = st.session_state.pool_type

if pool_type == "NFL Survivor":
    TARGET_SHEET_NAME = "NFL"
    API_URL = "http://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
else:
    TARGET_SHEET_NAME = "NCAA"
    API_URL = "http://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"

# --- 9. AUTO-UPDATING DASHBOARD FRAGMENT ---
@st.fragment(run_every=60)
def render_live_dashboard(sheet_name, api_url, pool_type, api_param, current_user, pick_col, group_id=50):
    st.write("### 🏆 Live Dashboard")
    df_scores_pick, _, fetch_time = get_sports_data(api_url, pool_type, api_param, group_id=group_id)
    df = load_data(sheet_name)
    
    st.caption(f"Last updated: {fetch_time} (Auto-refreshes every 60s)")

    if df.empty:
        st.warning("No data found.")
        return
    
    # SAFETY: Ensure Status column exists before proceeding
    if 'Status' not in df.columns:
        st.error("Waiting for players to join...")
        return

    active_grading_col = None
    active_grading_scores = pd.DataFrame()
    
    if api_param:
        active_grading_col = pick_col
        active_grading_scores = df_scores_pick

    # --- A. PICK DISTRIBUTION ---
    if active_grading_col and not active_grading_scores.empty:
        st.markdown(f"#### 📊 Distribution for {active_grading_col}")

        if 'Status' in df.columns:
            alive_df = df[df['Status'] == 'Alive']
            if active_grading_col in alive_df.columns:
                valid_picks = alive_df[active_grading_col]
                valid_picks = valid_picks[valid_picks != ""]

                dist_counts = valid_picks.value_counts().reset_index()
                dist_counts.columns = ["Team", "Count"]

                round_eliminated = 0
                round_safe = 0
                round_pending = 0

                for team, count in zip(dist_counts['Team'], dist_counts['Count']):
                    match = active_grading_scores[active_grading_scores['Team A'].str.contains(team, case=False, regex=False) |
                                        active_grading_scores['Team B'].str.contains(team, case=False, regex=False)]
                    if not match.empty:
                        game = match.iloc[0]
                        if game['Status'] == 'Final':
                            if team.lower() in game['Winner'].lower(): round_safe += count
                            elif game['Winner'] == "Tie": round_safe += count
                            else: round_eliminated += count
                        else: round_pending += count
                    else: round_pending += count

                m1, m2, m3 = st.columns(3)
                m1.metric("✅ Safe / Pending", f"{round_safe + round_pending}")
                m2.metric("💀 Eliminated Today", f"{round_eliminated}")
                m3.metric("📅 Total Picks", f"{valid_picks.count()}")

                if not dist_counts.empty:
                    def get_team_status_text(t_name):
                        m = active_grading_scores[active_grading_scores['Team A'].str.contains(t_name, case=False, regex=False) |
                                                  active_grading_scores['Team B'].str.contains(t_name, case=False, regex=False)]
                        if not m.empty:
                            g = m.iloc[0]
                            if g['Status'] == 'Final':
                                if t_name.lower() in g['Winner'].lower(): return "✅ Won"
                                return "❌ Lost"
                            return f"⏳ {g['Score']}"
                        return "❓"

                    dist_counts['Status'] = dist_counts['Team'].apply(get_team_status_text)
                    def highlight_losing_teams(row):
                        if "❌ Lost" in str(row['Status']): return ['background-color: #ffcccc'] * len(row)
                        return [''] * len(row)
                    st.dataframe(dist_counts.style.apply(highlight_losing_teams, axis=1), use_container_width=True, hide_index=True)

    st.divider()

    # --- B. LIVE STANDINGS TABLE ---
    if not df.empty and 'Name' in df.columns:
        pick_cols = [c for c in df.columns if ("Week" in c and pool_type == "NFL Survivor") or ("Day" in c and "March" in pool_type)]
        if not pick_cols: pick_cols = [c for c in df.columns if "Day" in c or "Week" in c]

        base_cols = ['Name', 'Status'] + pick_cols
        base_cols = [c for c in base_cols if c in df.columns]
        
        if not df.empty:
            df['Sort_Key'] = df['Status'].apply(lambda x: 1 if x == 'Eliminated' else 0)
            df_sorted = df.sort_values(by=['Sort_Key', 'Name'], ascending=[True, True])
            display_df = df_sorted[base_cols].copy()

            if not active_grading_scores.empty and active_grading_col and active_grading_col in display_df.columns:
                statuses = []
                for _, row in display_df.iterrows():
                    if row['Status'] == 'Eliminated':
                        statuses.append("ELIMINATED")
                        continue

                    pick = str(row.get(active_grading_col, "")).strip()
                    match = active_grading_scores[active_grading_scores['Team A'].str.contains(pick, case=False, regex=False) |
                                        active_grading_scores['Team B'].str.contains(pick, case=False, regex=False)]

                    res_text = "Pending"
                    if not match.empty:
                        game = match.iloc[0]
                        if game['Status'] == 'Final':
                            if pick.lower() in game['Winner'].lower(): res_text = "SAFE"
                            elif game['Winner'] == "Tie": res_text = "TIE"
                            else:
                                res_text = "ELIMINATED"
                                if row['Name'] == current_user and row['Status'] == 'Alive':
                                    update_player_status(sheet_name, row['Name'], "Eliminated")
                                    st.rerun()
                        else: res_text = f"In Progress ({game['Score']})"
                    elif pick == "": res_text = "No Pick"
                    else: res_text = "Unknown"
                    statuses.append(res_text)

                display_df['Latest Result'] = statuses
            else:
                display_df['Latest Result'] = display_df['Status'].apply(lambda x: "ELIMINATED" if x == "Eliminated" else "Waiting")

            def color_row(row):
                if row['Status'] == 'Eliminated': return ['background-color: #ffcccc'] * len(row)
                res = str(row.get('Latest Result', ''))
                if 'ELIMINATED' in res: return ['background-color: #ffcccc'] * len(row)
                if 'SAFE' in res: return ['background-color: #ccffcc'] * len(row)
                return [''] * len(row)

            st.caption("Standings sorted by Status (Alive first).")
            st.dataframe(display_df.style.apply(color_row, axis=1), use_container_width=True)

# --- 10. MAIN APP LOGIC ---
with st.sidebar:
    st.write("⚙️ **Settings**")
    app_mode = st.selectbox("View Mode", ["Player Portal", "Admin Access"])
    st.divider()
    if 'current_user' in st.session_state:
        st.write(f"Logged in as: **{st.session_state.current_user}**")
        if st.button("Log Out"):
            del st.session_state['current_user']
            st.rerun()

if app_mode == "Player Portal":
    if 'current_user' not in st.session_state:
        st.header(f"🔐 Login to {pool_type}")
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            tab1, tab2 = st.tabs(["🔑 Log In", "📝 Register"])
            with tab1:
                with st.form("login_form"):
                    email_input = st.text_input("Email Address")
                    password_input = st.text_input("Password", type="password")
                    if st.form_submit_button("Log In", use_container_width=True):
                        st.cache_data.clear()
                        df = load_data(TARGET_SHEET_NAME)
                        if not df.empty and 'Email' in df.columns:
                            clean_input = email_input.lower().strip()
                            clean_sheet_emails = df['Email'].astype(str).str.lower().str.strip()
                            if clean_input in clean_sheet_emails.values:
                                user_row = df[clean_sheet_emails == clean_input].iloc[0]
                                if check_hashes(password_input, str(user_row['Security_Hash'])):
                                    st.session_state.current_user = user_row['Name']
                                    st.rerun()
                                else: st.error("Incorrect Password.")
                            else: st.error(f"Email not found in {pool_type} pool.")
                        else: st.error("Pool is empty or data error.")
            with tab2:
                st.caption(f"Join the {pool_type} Pool")
                with st.form("reg"):
                    n = st.text_input("Name")
                    e = st.text_input("Email")
                    p = st.text_input("Create Password", type="password")
                    if st.form_submit_button("Join Pool", use_container_width=True):
                        if n and e and p:
                            if register_user(TARGET_SHEET_NAME, n, e, p)[0]:
                                st.cache_data.clear(); st.success("Registered! Please Log In.")
                            else: st.error("Registration failed.")
                        else: st.warning("Please fill all fields.")
    else:
        st.subheader(f"👋 {st.session_state.current_user} | {pool_type}")
        df = load_data(TARGET_SHEET_NAME)

        if df.empty or 'Status' not in df.columns:
            st.warning("⚠️ The game sheet is currently being initialized by the Admin. Please check back shortly.")
        else:
            try:
                user_row = df[df['Name'] == st.session_state.current_user].iloc[0]
            except IndexError:
                del st.session_state['current_user']
                st.rerun()

            current_status = user_row.get('Status', 'Alive')
            
            pick_cols = []
            if pool_type == "NFL Survivor":
                pick_cols = [c for c in df.columns if "Week" in c]
            else:
                pick_cols = [c for c in df.columns if "Day" in c]

            if not pick_cols: 
                st.error("No Round Columns Found in Sheet.")
            else:
                with st.expander("📅 Make Your Pick", expanded=True):
                    col_p1, col_p2 = st.columns([1, 1])
                    with col_p1: pick_col = st.selectbox("Select Round", pick_cols)

                    api_param = None
                    display_date = ""
                    group_id_filter = 50 

                    if pool_type == "NFL Survivor":
                        try:
                            week_num = int(re.search(r'\d+', pick_col).group())
                            api_param = week_num
                            display_date = f"NFL Week {week_num}"
                        except: display_date = "Unknown Week"
                    else:
                        config_map = get_round_config(TARGET_SHEET_NAME)
                        round_info = config_map.get(pick_col, {})
                        target_date_str = round_info.get('date')
                        target_group_id = round_info.get('group', 50)
                        
                        if target_date_str:
                            api_param = target_date_str.replace("-", "")
                            display_date = target_date_str
                            group_id_filter = target_group_id
                            group_name = "Div I"
                            for k, v in CONFERENCE_MAP.items():
                                if v == group_id_filter: group_name = k
                            display_date = f"{display_date} ({group_name})"
                        else:
                            display_date = None

                    df_scores_pick = pd.DataFrame()
                    universal_lock = None

                    with col_p2:
                        if display_date and api_param:
                            st.info(f"Date: **{display_date}**")
                            df_scores_pick, universal_lock, _ = get_sports_data(API_URL, pool_type, api_param, group_id=group_id_filter)
                        elif pool_type == "March Madness (NCAA)":
                            st.warning("⚠️ Admin has not scheduled this round yet.")

                    is_locked = False
                    if universal_lock:
                        if not df_scores_pick.empty and any(df_scores_pick['Status'].isin(['In Progress', 'Final', 'Halftime', 'End of Period'])):
                            is_locked = True
                        elif datetime.now(timezone.utc) >= universal_lock:
                            is_locked = True

                    if current_status == "Eliminated":
                        st.error("💀 **You have been Eliminated.**")
                        st.write(f"Your pick for {pick_col}: {user_row.get(pick_col, 'None')}")
                    elif not api_param:
                        st.write("Round not configured.")
                    elif is_locked:
                        st.warning(f"🔒 **Picks Locked for {pick_col}**")
                        st.write(f"Your pick: **{user_row.get(pick_col, 'No Pick')}**")
                    else:
                        past_picks = [str(user_row[c]) for c in pick_cols if c != pick_col and str(user_row[c])]
                        if past_picks: st.markdown(f"**🚫 Teams Used:** {', '.join(past_picks)}")
                        else: st.caption("🚫 Teams Used: None")

                        available_teams = []
                        if not df_scores_pick.empty:
                            teams_playing = df_scores_pick[['Team A', 'Team B']].to_dict('records')
                            for game in teams_playing:
                                if game['Team A'] not in past_picks: available_teams.append(game['Team A'])
                                if game['Team B'] not in past_picks: available_teams.append(game['Team B'])
                        
                        available_teams = sorted(list(set(available_teams)))
                        et_time = to_eastern(universal_lock)
                        if et_time: st.caption(f"Lock time: {et_time.strftime('%I:%M %p ET')}")

                        with st.form("pick_form"):
                            selection = st.selectbox("Choose Team", [""] + available_teams)
                            if st.form_submit_button("Submit Pick"):
                                if selection:
                                    if save_pick_to_sheet(TARGET_SHEET_NAME, st.session_state.current_user, pick_col, selection):
                                        st.success("Saved!"); time.sleep(1); st.rerun()
                                    else: st.error("Save failed.")
                                else: st.error("Please select a team.")

                st.divider()
                render_live_dashboard(
                    sheet_name=TARGET_SHEET_NAME,
                    api_url=API_URL,
                    pool_type=pool_type,
                    api_param=api_param,
                    current_user=st.session_state.current_user,
                    pick_col=pick_col,
                    group_id=group_id_filter
                )

elif app_mode == "Admin Access":
    with st.sidebar:
        st.divider()
        st.header("🔐 Admin Authorization")
        admin_pass = st.text_input("Admin Password", type="password")

    if admin_pass == "admin123":
        st.header(f"🛠️ Admin Dashboard: {pool_type}")
        
        if pool_type == "March Madness (NCAA)":
            ensure_config_sheet(TARGET_SHEET_NAME)

        # CHECK 1: DOES SHEET EXIST?
        sheet_missing = not check_sheet_exists(TARGET_SHEET_NAME)
        
        # CHECK 2: LOAD DATA ROBUSTLY
        df = load_data(TARGET_SHEET_NAME)
        
        # CHECK 3: DO HEADERS EXIST?
        if df.empty and len(df.columns) == 0:
            st.error(f"⚠️ The '{TARGET_SHEET_NAME}' sheet is missing headers.")
            if st.button(f"🛠️ Initialize '{TARGET_SHEET_NAME}' Sheet"):
                if initialize_sheet_headers(TARGET_SHEET_NAME, pool_type):
                    st.success("Sheet initialized successfully! Reloading...")
                    st.cache_data.clear()
                    time.sleep(1)
                    st.rerun()
            st.stop() 

        # --- NORMAL ADMIN FUNCTION ---
        pick_col_search = [c for c in df.columns if "Day" in c or "Week" in c]

        if pool_type == "March Madness (NCAA)":
            with st.expander("📅 Set Round Dates & Tournament Mode", expanded=True):
                c1, c2, c3 = st.columns([2, 2, 2])
                with c1: target_round = st.selectbox("Select Round", pick_col_search)
                with c2: set_date = st.date_input("Assign Date", datetime.now())
                with c3: 
                    conf_name = st.selectbox("League / Tournament", list(CONFERENCE_MAP.keys()), index=0)
                    group_id_val = CONFERENCE_MAP[conf_name]

                if st.button("Save Date & League"):
                    set_round_config(TARGET_SHEET_NAME, target_round, str(set_date), group_id_val)
                    st.success(f"Linked {target_round} to {set_date} (Group: {conf_name})")
                    st.cache_data.clear()

        # --- NEW: EMAIL LIST ---
        with st.expander("📧 Player Emails (Copy/Paste)", expanded=False):
            if not df.empty and 'Email' in df.columns:
                hide_elim = st.checkbox("Hide Eliminated Players from list")
                
                email_df = df.copy()
                if hide_elim:
                    email_df = email_df[email_df['Status'] != 'Eliminated']
                
                unique_emails = [e for e in email_df['Email'].unique() if str(e).strip()]
                email_str = ", ".join(unique_emails)
                st.text_area("Copy this list:", value=email_str, height=100)
                st.caption(f"Total Emails: {len(unique_emails)}")
            else:
                st.warning("No email data found.")

        st.divider()

        col1, col2 = st.columns([2, 1])

        with col1:
            st.subheader("Live Standings (Auto-Grader)")
            if not pick_col_search:
                st.warning("No Day/Week columns found.")
            else:
                admin_view_col = st.selectbox("Select Round to Grade", pick_col_search)
                
                admin_api_param = None
                admin_group_id = 50 

                if pool_type == "NFL Survivor":
                    try: admin_api_param = int(re.search(r'\d+', admin_view_col).group())
                    except: pass
                else:
                    config_map = get_round_config(TARGET_SHEET_NAME)
                    round_data = config_map.get(admin_view_col, {})
                    d_str = round_data.get('date')
                    admin_group_id = round_data.get('group', 50)
                    if d_str: admin_api_param = d_str.replace("-", "")

                df_scores = pd.DataFrame()
                if admin_api_param:
                    st.caption(f"Grading using param: {admin_api_param} | Group ID: {admin_group_id}")
                    df_scores, _, _ = get_sports_data(API_URL, pool_type, admin_api_param, group_id=admin_group_id)
                else: st.error("❌ No date/week set for this round.")

                # SAFE SORTING
                if not df.empty:
                    df['Sort_Key'] = df['Status'].apply(lambda x: 1 if x == 'Eliminated' else 0)
                    display_df = df.sort_values(by=['Sort_Key', 'Name']).drop(columns=['Security_Hash', 'Email', 'Sort_Key'], errors='ignore')
                else:
                    display_df = pd.DataFrame(columns=df.columns)

                players_updated_count = 0

                if not df_scores.empty and admin_view_col and not display_df.empty:
                    sh = get_google_spreadsheet()
                    ws = sh.worksheet(TARGET_SHEET_NAME)
                    stat_col_idx = ws.find("Status").col

                    for idx, row in display_df.iterrows():
                        pick = str(row.get(admin_view_col, "")).strip()
                        current_status = row.get("Status", "Alive")
                        if current_status == "Eliminated": continue

                        match = df_scores[df_scores['Team A'].str.contains(pick, case=False, regex=False) |
                                            df_scores['Team B'].str.contains(pick, case=False, regex=False)]

                        if not match.empty:
                            game = match.iloc[0]
                            new_status = current_status
                            if game['Status'] == 'Final':
                                is_winner = pick.lower() in game['Winner'].lower() or "tie" in game['Winner'].lower()
                                if not is_winner: new_status = "Eliminated"
                                else: new_status = "Alive"
                            
                            if new_status != current_status:
                                try:
                                    cell = ws.find(row['Name'])
                                    ws.update_cell(cell.row, stat_col_idx, new_status)
                                    players_updated_count += 1
                                except: pass

                    if players_updated_count > 0:
                        st.toast(f"✅ Auto-Updated Status for {players_updated_count} players!", icon="🔄")
                        st.cache_data.clear(); time.sleep(1.0); st.rerun()

                def color_status(val): return 'color: red; font-weight: bold' if val == "Eliminated" else 'color: green'
                st.dataframe(display_df.style.applymap(color_status, subset=['Status']), use_container_width=True)

        with col2:
            st.subheader("Manual Override")
            with st.form("admin_manage"):
                p_name = st.selectbox("Player", df['Name'].unique() if 'Name' in df.columns else [])
                p_col = st.selectbox("Column to Edit", pick_col_search if pick_col_search else ["No Cols"])
                teams = []
                if not df_scores.empty:
                    teams = sorted(list(set(df_scores['Team A'].tolist() + df_scores['Team B'].tolist())))
                p_team = st.selectbox("Set Team", [""] + teams)

                if st.form_submit_button("Update Pick & Status"):
                    if p_team:
                        sh = get_google_spreadsheet()
                        ws = sh.worksheet(TARGET_SHEET_NAME)
                        cell = ws.find(p_name)
                        c_idx = ws.find(p_col).col
                        ws.update_cell(cell.row, c_idx, p_team)

                        new_status_manual = "Alive"
                        if not df_scores.empty:
                            match = df_scores[df_scores['Team A'].str.contains(p_team, case=False, regex=False) |
                                                                df_scores['Team B'].str.contains(p_team, case=False, regex=False)]
                            if not match.empty:
                                game = match.iloc[0]
                                if game['Status'] == 'Final':
                                    is_winner = p_team.lower() in game['Winner'].lower() or "tie" in game['Winner'].lower()
                                    if not is_winner: new_status_manual = "Eliminated"

                        stat_col_idx = ws.find("Status").col
                        ws.update_cell(cell.row, stat_col_idx, new_status_manual)
                        st.success(f"Updated {p_name} to {p_team} -> Status: {new_status_manual}")
                        st.cache_data.clear(); time.sleep(1.5); st.rerun()
                    else: st.warning("Please select a team.")

    elif admin_pass: st.error("Wrong Password")
