import streamlit as st
import pandas as pd
import gspread
import requests
import time
import hashlib
import re
import os
import smtplib # Make sure this is here
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- NEW: RETRY LOGIC FOR 400 USERS ---
from tenacity import retry, stop_after_attempt, wait_exponential

# This tells the code: "If Google fails, wait 2s, then 4s, then 8s... up to 5 times"
retry_spreadsheet = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
# --------------------------------------

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

# --- 3. SECURITY & EMAIL FUNCTIONS ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def check_hashes(password, hashed_text):
    if make_hashes(password) == hashed_text:
        return True
    return False

def send_confirmation_email(to_email, user_name):
    """Sends a welcome email using templates from the Settings sheet."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if "email" not in st.secrets:
        return False, "Email secrets not configured."

    sender_email = st.secrets["email"]["address"]
    sender_password = st.secrets["email"]["password"]
    smtp_server = "smtp.gmail.com"
    smtp_port = 587

    # --- 1. FETCH CUSTOM TEMPLATE FROM SHEETS ---
    # B2 = Subject, B3 = Body
    try:
        sh = get_google_spreadsheet()
        # We assume 'Settings' tab exists (created for Global Banner)
        ws = sh.worksheet("Settings")
        custom_subject = ws.acell('B2').value
        custom_body = ws.acell('B3').value
    except:
        custom_subject = None
        custom_body = None

    # --- 2. PREPARE CONTENT ---
    subject = custom_subject if custom_subject else "Welcome to the Survivor Pool! 🏀"
    
    # Handle {name} placeholder
    if custom_body:
        body = custom_body.replace("{name}", user_name)
    else:
        # Default Fallback
        body = f"""
        Hi {user_name},

        You have successfully registered for the Survivor Pool!
        
        Log in now to make your picks.
        
        Good luck!
        - The Commish
        """

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, to_email, text)
        server.quit()
        return True, "Email sent"
    except Exception as e:
        return False, str(e)

@retry_spreadsheet
def batch_eliminate_players(sheet_name, names_to_eliminate):
    """
    Updates the Status of multiple players to 'Eliminated' in one API call.
    """
    if not names_to_eliminate: return False
    
    try:
        sh = get_google_spreadsheet() 
        ws = sh.worksheet(sheet_name)
        
        # Get all data to find Row Numbers
        all_names = ws.col_values(1) 
        
        # Find 'Status' column
        header_row = ws.row_values(1)
        try:
            status_col_idx = header_row.index("Status") + 1 
        except ValueError:
            st.error("Could not find 'Status' column in sheet.")
            return False

        updates = []
        
        # Build the batch update list
        for name in names_to_eliminate:
            if name in all_names:
                row_num = all_names.index(name) + 1 
                updates.append({
                    'range': gspread.utils.rowcol_to_a1(row_num, status_col_idx),
                    'values': [['Eliminated']]
                })
        
        # Execute the Batch Update
        if updates:
            # --- FIX IS HERE: Pass 'updates' list directly, not inside a dict ---
            ws.batch_update(updates) 
            return True
            
    except Exception as e:
        st.error(f"Auto-Update Failed: {e}")
        return False
    
    return False

# --- FETCH GLOBAL BANNER FUNCTION ---
@st.cache_data(ttl=300) # Cache for 5 mins to save API hits
def get_global_banner():
    try:
        sh = get_google_spreadsheet()
        # Ensure you created a tab named "Settings" in your Google Sheet
        ws = sh.worksheet("Settings") 
        return ws.acell('B1').value
    except:
        return None

# --- 4. GOOGLE SHEETS FUNCTIONS ---
@st.cache_resource
def get_google_spreadsheet():
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(current_dir, 'service_account.json')
        gc = gspread.service_account(filename=json_path)
    except FileNotFoundError:
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
            # Better Method: Get a list of all sheet titles first
            existing_titles = [ws.title for ws in sh.worksheets()]
            
            # Only try to create it if it's strictly NOT in the list
            if config_name not in existing_titles:
                ws = sh.add_worksheet(title=config_name, rows=50, cols=3)
                ws.update([["Round_Name", "Date_String", "Group_ID"]], 'A1')
        except Exception as e:
            # If we get an error here, it means the API is just busy. 
            # We do nothing because the sheet likely exists or will work on next reload.
            print(f"Config Check Log: {e}")
            pass

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

@st.cache_data(ttl=60)
def load_data(sheet_name):
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

def batch_update_tiebreakers(sheet_name, date_list_str):
    if not date_list_str: return False, "No dates configured."
    date_list = [d.strip() for d in date_list_str.split(",") if d.strip()]
    seed_map = {}
    url = "http://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
    
    try:
        for d_str in date_list:
            target_url = f"{url}?dates={d_str}&groups=100&limit=200"
            resp = requests.get(target_url).json()
            if 'events' in resp:
                for event in resp['events']:
                    comps = event['competitions'][0]['competitors']
                    for c in comps:
                        team = c['team']['displayName']
                        rank = c.get('curatedRank', {}).get('current', 99)
                        seed = int(rank) if rank != 99 else 0
                        seed_map[team] = seed
    except Exception as e:
        return False, f"API Error: {str(e)}"

    if not seed_map:
        return False, "No seed data found for those dates."

    sh = get_google_spreadsheet()
    ws = sh.worksheet(sheet_name)
    df = pd.DataFrame(ws.get_all_records())
    
    if 'Tiebreaker' not in df.columns:
        return False, "Column 'Tiebreaker' missing. Please re-initialize headers."

    updates = []
    meta = ["Name", "Email", "Phone", "Security_Hash", "Status", "Paid", "Tiebreaker", "Sort_Key"]
    pick_cols = [c for c in df.columns if c not in meta]

    tiebreaker_col_idx = ws.find("Tiebreaker").col

    for i, row in df.iterrows():
        total_score = 0
        for col in pick_cols:
            pick = str(row[col]).strip()
            if pick in seed_map:
                total_score += seed_map[pick]
        
        updates.append({
            'range': gspread.utils.rowcol_to_a1(i + 2, tiebreaker_col_idx),
            'values': [[total_score]]
        })

    if updates:
        ws.batch_update({'valueInputOption': 'RAW', 'data': updates})
        return True, f"Updated {len(updates)} players with latest seed scores."
    
    return True, "No updates needed."

# --- MODIFIED: HEADER NAMES ---
def initialize_sheet_headers(sheet_name, pool_type):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=100, cols=25)
    
    base_headers = ["Name", "Email", "Phone", "Security_Hash", "Status", "Paid", "Tiebreaker"]
    
    if pool_type == "NFL Survivor":
        headers = base_headers + [f"Week {i}" for i in range(1,19)]
    else:
        rounds = [
            "Round of 64 (Day 1)", "Round of 64 (Day 2)",
            "Round of 32 (Day 1)", "Round of 32 (Day 2)",
            "Sweet Sixteen (Day 1)", "Sweet Sixteen (Day 2)",
            "Elite Eight (Day 1)", "Elite Eight (Day 2)",
            "Final Four", "Championship"
        ]
        headers = base_headers + rounds
    
    ws.clear()
    ws.update([headers], 'A1')
    return True

def to_eastern(utc_dt):
    if utc_dt is None: return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(ZoneInfo("America/New_York"))


# --- 5. DATA FETCHER (WITH LOGOS & TIMEZONE FIX) ---
@st.cache_data(ttl=60)
def get_sports_data(base_url, pool_type, param, group_id=50):
    # Fix: Ensure default "Today" is Eastern Time, not UTC
    if not param:
        param = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")

    fetch_time = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p")
    target_url = base_url
    
    if pool_type == "NFL Survivor":
        target_url = f"{base_url}?week={param}&seasontype=2"
    elif pool_type == "March Madness (NCAA)":
        target_url = f"{base_url}?dates={param}&groups={group_id}&limit=200"

    try:
        response = requests.get(target_url)
        data = response.json()
        games = []
        earliest_start = None

        if 'events' in data:
            for event in data['events']:
                short_status = event['status']['type']['description']
                # NEW: Get detailed status (Time/Half)
                status_detail = event['status']['type']['detail'] 
                state = event['status']['type']['state']
                date_str = event['date']
                
                # Timezone parsing fix
                game_time = None
                for fmt in ["%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"]:
                    try:
                        game_time = datetime.strptime(date_str, fmt)
                        game_time = game_time.replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                
                if game_time is None: continue 

                if earliest_start is None or game_time < earliest_start:
                    earliest_start = game_time

                comps = event['competitions'][0]['competitors']
                
                # NEW: Grab Logo URLs
                logo_0 = comps[0]['team'].get('logo', 'https://a.espncdn.com/combiner/i?img=/i/teamlogos/ncaa/500/scoreboard/ncaa.png&h=40&w=40')
                logo_1 = comps[1]['team'].get('logo', 'https://a.espncdn.com/combiner/i?img=/i/teamlogos/ncaa/500/scoreboard/ncaa.png&h=40&w=40')
                
                seed_0 = comps[0].get('curatedRank', {}).get('current', 99)
                seed_1 = comps[1].get('curatedRank', {}).get('current', 99)
                
                team_0 = comps[0]['team']['displayName']
                team_1 = comps[1]['team']['displayName']
                score_0 = int(comps[0].get('score', 0))
                score_1 = int(comps[1].get('score', 0))

                winner = "TBD"
                if state == "post":
                    if score_0 > score_1: winner = team_0
                    elif score_1 > score_0: winner = team_1
                    else: winner = "Tie"
                
                is_locked_game = state in ["in", "post"]

                games.append({
                    "Team A": team_0, "Team B": team_1, "Winner": winner,
                    "Status": short_status, "Detail": status_detail, # Saved Detail
                    "Score": f"{score_0}-{score_1}",
                    "ScoreA_Int": score_0, "ScoreB_Int": score_1,    # Saved Ints
                    "Locked": is_locked_game,
                    "StartTime": game_time,
                    "Seed A": seed_0, "Logo A": logo_0, # Saved Logo
                    "Seed B": seed_1, "Logo B": logo_1  # Saved Logo
                })
        return pd.DataFrame(games), earliest_start, fetch_time
    except Exception as e:
        return pd.DataFrame(), None, fetch_time

# --- 6. USER FUNCTIONS ---
@retry_spreadsheet
def register_user(sheet_name, name, email, phone, password):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
        existing_data = pd.DataFrame(ws.get_all_records())
        
        new_email = email.lower().strip()
        new_name = name.lower().strip()

        if not existing_data.empty:
            if 'Email' in existing_data.columns:
                existing_emails = existing_data['Email'].astype(str).str.lower().str.strip().values
                if new_email in existing_emails:
                    return False, "❌ This Email is already registered!"
            
            if 'Name' in existing_data.columns:
                existing_names = existing_data['Name'].astype(str).str.lower().str.strip().values
                if new_name in existing_names:
                    return False, "❌ This Name is already taken! Please add an initial or use a unique nickname."
        
        ws.append_row([name, email, phone, make_hashes(password), "Alive", "FALSE", 0])
        
        try:
            send_confirmation_email(email, name)
        except:
            pass 
        
        return True, "Success"
    except Exception as e: return False, str(e)

@retry_spreadsheet
def save_pick_to_sheet(sheet_name, player_name, week_col, team_pick):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
        cell = ws.find(player_name)
        col_idx = ws.find(week_col).col
        ws.update_cell(cell.row, col_idx, team_pick)
        return True
    except: return False

@retry_spreadsheet
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
st.session_state.pool_type = "March Madness (NCAA)"
pool_type = st.session_state.pool_type

if pool_type == "NFL Survivor":
    TARGET_SHEET_NAME = "NFL"
    API_URL = "http://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
else:
    TARGET_SHEET_NAME = "NCAA"
    API_URL = "http://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"

# --- 9. MANUAL DASHBOARD (No Auto-Refresh) ---
# We removed @st.fragment so it only runs when the user interacts or clicks the button.
def render_live_dashboard(sheet_name, api_url, pool_type, api_param, current_user, pick_col, group_id=50, universal_lock_time=None):
    
    # --- 1. HEADER & REFRESH BUTTON ---
    # This button replaces the auto-timer. It gives the user control.
    c_head_1, c_head_2 = st.columns([3, 1])
    with c_head_1:
        st.subheader("🏆 Live Dashboard")
    with c_head_2:
        if st.button("🔄 Refresh Scores", use_container_width=True):
            st.rerun()

    # --- 0. MEMORY LATCH (Kept for safety) ---
    cache_key = f"sticky_group_id_{pick_col.strip()}"
    active_group_id = st.session_state.get(cache_key, group_id)

    try:
        config_map = get_round_config(sheet_name)
        if config_map:
            clean_map = {k.strip(): v for k, v in config_map.items()}
            clean_pick = pick_col.strip()

            if clean_pick in clean_map:
                new_group = clean_map[clean_pick].get('group')
                if new_group:
                    active_group_id = int(new_group)
                    st.session_state[cache_key] = active_group_id
    except Exception:
        pass

    # --- 1. Fetch Fresh Data ---
    df_scores_pick, fresh_lock, fetch_time = get_sports_data(api_url, pool_type, api_param, group_id=active_group_id)
    df = load_data(sheet_name)
    
    if 'view_date_param' in st.session_state and st.session_state['view_date_param']:
        api_param = st.session_state['view_date_param']
        
    display_date = api_param if api_param else f"Today (Auto: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')})"
    st.caption(f"📅 Viewing: {display_date} | 🏷️ Round: {pick_col} | ⏳ Last updated: {fetch_time}")

    if df.empty:
        st.warning("No player data found.")
        return

    # --- A. TV STYLE SCOREBOARD (Mobile Optimized) ---
    if df_scores_pick.empty:
        st.info(f"No games scheduled for {display_date} (Group ID: {active_group_id}).")
    else:
        for _, game in df_scores_pick.iterrows():
            # Use 3 columns instead of 5. This prevents the "messy stack" on mobile.
            # Ratio: 40% (Team A) - 20% (Score) - 40% (Team B)
            c_a, c_score, c_b = st.columns([4, 2, 4])
            
            s_a = game.get('ScoreA_Int', 0)
            s_b = game.get('ScoreB_Int', 0)
            
            # TEAM A (Left Side) - Aligned to the RIGHT (next to score)
            # We use HTML to lock the Name and Logo on the same line.
            with c_a:
                logo_a = game.get('Logo A', '')
                # HTML: Name then Logo (Img)
                st.markdown(
                    f"""<div style='text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;'>
                        <b>{game['Team A']}</b>
                        <img src='{logo_a}' style='height: 30px; vertical-align: middle; margin-left: 8px;'>
                    </div>""", 
                    unsafe_allow_html=True
                )
            
            # SCORE (Center)
            with c_score:
                st.markdown(f"<h4 style='text-align: center; margin: 0;'>{s_a} - {s_b}</h4>", unsafe_allow_html=True)
                
                status_txt = game.get('Status', '')
                detail_txt = game.get('Detail', '')
                
                # If status is long (e.g. "Final/OT"), keep it small so it fits
                time_color = "#FF4B4B" if "In Progress" in status_txt or "Half" in status_txt else "#808495"
                st.markdown(f"<div style='text-align: center; color: {time_color}; font-size: 0.75rem; line-height: 1.1;'>{status_txt}</div>", unsafe_allow_html=True)

            # TEAM B (Right Side) - Aligned to the LEFT (next to score)
            # HTML: Logo (Img) then Name
            with c_b:
                logo_b = game.get('Logo B', '')
                st.markdown(
                    f"""<div style='text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;'>
                        <img src='{logo_b}' style='height: 30px; vertical-align: middle; margin-right: 8px;'>
                        <b>{game['Team B']}</b>
                    </div>""", 
                    unsafe_allow_html=True
                )
            
            st.divider()

    # --- BLIND PICK LOGIC ---
    is_current_revealed = False
    if not df_scores_pick.empty and any(df_scores_pick['Status'].isin(['In Progress', 'Final', 'Halftime', 'End of Period'])):
        is_current_revealed = True
    
    check_lock = fresh_lock if fresh_lock else universal_lock_time
    if check_lock and isinstance(check_lock, datetime):
        if datetime.now(timezone.utc) >= check_lock:
            is_current_revealed = True
            
    if not api_param: is_current_revealed = False 

    # --- B. PICK DISTRIBUTION ---
    if is_current_revealed:
        active_grading_col = pick_col if api_param else None
        active_grading_scores = df_scores_pick

        if active_grading_col and not active_grading_scores.empty and 'Team A' in active_grading_scores.columns:
            st.markdown(f"#### 📊 Distribution for {active_grading_col}")

            if active_grading_col in df.columns:
                valid_picks_series = df[df[active_grading_col].astype(str).str.strip() != ""][active_grading_col]
                dist_counts = valid_picks_series.value_counts().reset_index()
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
                            if team.lower() in game['Winner'].lower(): 
                                round_safe += count
                            elif game['Winner'] == "Tie": 
                                round_safe += count 
                            else: 
                                round_eliminated += count
                        else:
                            round_pending += count
                    else: 
                        round_pending += count

                historical_eliminated = df[df['Status'] == 'Eliminated'].shape[0]
                total_eliminated = historical_eliminated

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("✅ Safe (Final)", f"{round_safe}")
                m2.metric("⏳ Pending", f"{round_pending}")
                m3.metric("💀 Eliminated Today", f"{round_eliminated}")
                m4.metric("⚰️ Total Eliminated", f"{total_eliminated}")

                if not dist_counts.empty:
                    def get_team_status_text(t_name):
                        m = active_grading_scores[active_grading_scores['Team A'].str.contains(t_name, case=False, regex=False) |
                                                                  active_grading_scores['Team B'].str.contains(t_name, case=False, regex=False)]
                        if not m.empty:
                            g = m.iloc[0]
                            if g['Status'] == 'Final':
                                if t_name.lower() in g['Winner'].lower(): return "✅ Won"
                                return "❌ Lost"
                            return "⏳ In Progress" 
                        return "❓"

                    dist_counts['Result'] = dist_counts['Team'].apply(get_team_status_text)
                    
                    def highlight_losing_teams(row):
                        if "❌ Lost" in str(row['Result']): return ['background-color: #ffcccc'] * len(row)
                        if "✅ Won" in str(row['Result']): return ['background-color: #ccffcc'] * len(row)
                        return [''] * len(row)
                    
                    cols = ['Team', 'Count', 'Result']
                    st.dataframe(
                        dist_counts[cols].style.apply(highlight_losing_teams, axis=1), 
                        use_container_width=True, 
                        hide_index=True
                    )
        elif not active_grading_scores.empty and 'Team A' not in active_grading_scores.columns:
            st.error("Error: Game data structure invalid.")
    else:
        st.info("🔒 Pick Distribution is hidden until games begin/lock.")

    st.divider()
    # --- C. LIVE STANDINGS TABLE ---
    if not df.empty and 'Name' in df.columns:
        display_df = df.copy()
        
        metadata_cols = ["Name", "Email", "Phone", "Security_Hash", "Status", "Paid", "Tiebreaker", "Sort_Key"]
        all_round_cols = [c for c in df.columns if c not in metadata_cols]
        today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        
        for col_name in all_round_cols:
            is_col_revealed_in_table = False
            if col_name == pick_col:
                is_col_revealed_in_table = is_current_revealed
            else:
                if col_name in config_map:
                    c_date = config_map[col_name]['date']
                    if c_date < today_str: is_col_revealed_in_table = True  
                    elif c_date > today_str: is_col_revealed_in_table = False 
                    else: is_col_revealed_in_table = False 
                else: is_col_revealed_in_table = False
            
            if not is_col_revealed_in_table:
                display_df[col_name] = display_df[col_name].apply(lambda x: "🔒 Hidden" if str(x).strip() != "" else "")

        cols_to_show = ['Name', 'Status', 'Tiebreaker']
        
        if is_current_revealed and pick_col in display_df.columns:
            cols_to_show.append(pick_col)
        
        for c in all_round_cols:
            if c != pick_col and c in display_df.columns:
                cols_to_show.append(c)

        display_df = display_df[cols_to_show]

        if is_current_revealed and pick_col in display_df.columns:
            active_grading_scores = df_scores_pick 
            
            if 'Team A' in active_grading_scores.columns:
                statuses = []
                for _, row in display_df.iterrows():
                    current_status = str(row.get('Status', '')).strip()
                    if current_status == "Eliminated":
                        statuses.append("Already Out") 
                        continue

                    pick = str(row.get(pick_col, "")).strip()
                    if "Hidden" in pick: 
                        statuses.append("Unknown")
                        continue
                    if pick == "": 
                        statuses.append("No Pick")
                        continue
                    
                    match = active_grading_scores[
                        active_grading_scores['Team A'].str.contains(pick, case=False, regex=False) | 
                        active_grading_scores['Team B'].str.contains(pick, case=False, regex=False)
                    ]
                    
                    if not match.empty:
                        game = match.iloc[0]
                        if game['Status'] == 'Final':
                            if pick.lower() in game['Winner'].lower(): 
                                statuses.append("SAFE")
                            elif game['Winner'] == "Tie": 
                                statuses.append("TIE")
                            else: 
                                statuses.append("ELIMINATED")
                        else: 
                            score_txt = game.get('Score', '')
                            statuses.append(f"In Progress ({score_txt})" if score_txt else "In Progress")
                    else: 
                        statuses.append("Unknown")
                
                display_df['Result'] = statuses
            else:
                display_df['Result'] = "Data Error"
        else: 
            display_df['Result'] = "Waiting for Lock"

        df['Sort_Key'] = df['Status'].apply(lambda x: 1 if x == 'Eliminated' else 0)
        display_df['Sort_Key'] = df['Sort_Key'] 
        display_df = display_df.sort_values(by=['Sort_Key', 'Name'], ascending=[True, True]).drop(columns=['Sort_Key'])

        def highlight_row(row):
            if row['Status'] == 'Eliminated': return ['background-color: #ffcccc'] * len(row)
            if 'Result' in row:
                if 'ELIMINATED' in str(row['Result']): return ['background-color: #ffcccc'] * len(row)
                if 'SAFE' in str(row['Result']): return ['background-color: #ccffcc'] * len(row)
            return [''] * len(row)
        
        candidates_for_elimination = []
        if is_current_revealed and 'Result' in display_df.columns and 'Status' in display_df.columns:
            for index, row in display_df.iterrows():
                if row['Status'] == 'Alive' and 'ELIMINATED' in str(row['Result']):
                    candidates_for_elimination.append(row['Name'])
    
        if candidates_for_elimination:
            msg = st.empty()
            msg.info(f"🔄 Games went final. Updating standings for {len(candidates_for_elimination)} players...")
            success = batch_eliminate_players(sheet_name, candidates_for_elimination)
            if success:
                msg.success("✅ Standings Updated! Refreshing...")
                time.sleep(1)
                st.cache_data.clear() 
                st.rerun()            

        st.dataframe(
            display_df.style.apply(highlight_row, axis=1), 
            use_container_width=True
        )
        

# --- 10. MAIN APP LOGIC ---
def main():
    # --- SIDEBAR & SETTINGS ---
    with st.sidebar:
        st.write("⚙️ **Settings**")
        app_mode = st.selectbox("View Mode", ["Player Portal", "Admin Access"])
        st.divider()
        if 'current_user' in st.session_state:
            st.write(f"Logged in as: **{st.session_state.current_user}**")
            if st.button("Log Out"):
                del st.session_state['current_user']
                if 'view_date_param' in st.session_state: del st.session_state['view_date_param']
                st.rerun()

    # --- A. PLAYER PORTAL ---
    if app_mode == "Player Portal":
        # --- GLOBAL ANNOUNCEMENT BANNER ---
        banner_msg = get_global_banner()
        if banner_msg:
            # st.info makes it a nice blue box. Use st.error for red, st.warning for yellow.
            st.info(f"{banner_msg}")
        if 'current_user' not in st.session_state:
            # ... (LOGIN / REGISTER LOGIC REMAINS THE SAME) ...
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
                        ph = st.text_input("Phone Number")
                        p = st.text_input("Create Password", type="password")
                        if st.form_submit_button("Join Pool", use_container_width=True):
                            if n and e and p:
                                if register_user(TARGET_SHEET_NAME, n, e, ph, p)[0]:
                                    st.cache_data.clear(); st.success("Registered! Please Log In.")
                                else: st.error("Registration failed.")
                            else: st.warning("Please fill all fields.")
        else:
            # --- LOGGED IN VIEW ---
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
                
                metadata_cols = ["Name", "Email", "Phone", "Security_Hash", "Status", "Paid", "Tiebreaker", "Sort_Key"]
                pick_cols = [c for c in df.columns if c not in metadata_cols]

                if not pick_cols: 
                    st.error("No Round Columns Found in Sheet.")
                else:
                    with st.expander("📅 Make Your Pick", expanded=True):
                        col_p1, col_p2 = st.columns([1, 1])
                        
                        # 1. Round Selection
                        with col_p1: 
                            # FIX: Calculate which item should be selected by default
                            default_index = 0
                            
                            # Check if we have a saved round in our "browser memory" (session_state)
                            if 'view_round_param' in st.session_state:
                                saved_round = st.session_state['view_round_param']
                                # If that saved round actually exists in our list, get its position number
                                if saved_round in pick_cols:
                                    default_index = pick_cols.index(saved_round)

                            # Create the dropdown, forcing it to start at our calculated 'default_index'
                            pick_col = st.selectbox("Select Round", pick_cols, index=default_index)

                        # 2. Calculate Dates/Groups
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
                            else:
                                display_date = None

                        # --- CRITICAL FIX START: SYNC SESSION STATE ---
                        # We force the session state to match the selection IMMEDIATELY.
                        # This prevents the dashboard from resetting to "Today" on refresh.
                        if api_param:
                            st.session_state['view_date_param'] = api_param
                            st.session_state['view_round_param'] = pick_col
                        # --- CRITICAL FIX END ---

                        # 3. Fetch Game Data
                        df_scores_pick, universal_lock, _ = get_sports_data(API_URL, pool_type, api_param, group_id=group_id_filter)
                        
                        local_logo_map = {}
                        if not df_scores_pick.empty:
                            for _, row in df_scores_pick.iterrows():
                                local_logo_map[row['Team A']] = row['Logo A']
                                local_logo_map[row['Team B']] = row['Logo B']

                        with col_p2:
                            if display_date and api_param:
                                st.info(f"Date: **{display_date}**")
                            elif pool_type == "March Madness (NCAA)":
                                st.warning("⚠️ Admin has not scheduled this round yet.")

                        # 4. Check Locks & Status
                        is_locked = False
                        if universal_lock:
                            if not df_scores_pick.empty and any(df_scores_pick['Status'].isin(['In Progress', 'Final', 'Halftime', 'End of Period'])):
                                is_locked = True
                            elif isinstance(universal_lock, datetime) and datetime.now(timezone.utc) >= universal_lock:
                                is_locked = True

                        # --- NEW: PREVIOUS ROUND VALIDATION ---
                        can_pick = True
                        block_msg = ""

                        # Get index of current round (e.g., 0, 1, 2)
                        current_idx = pick_cols.index(pick_col)

                        # If this is NOT the first round, check the previous one
                        if current_idx > 0:
                            prev_col = pick_cols[current_idx - 1]
                            prev_pick = str(user_row.get(prev_col, "")).strip()

                            # A. Did they make a pick?
                            if not prev_pick or prev_pick == "FALSE":
                                can_pick = False
                                block_msg = f"🚫 You cannot pick for **{pick_col}** because you missed **{prev_col}**."
                            else:
                                # B. Did that pick WIN?
                                # We need to fetch the scoreboard for the PREVIOUS round.
                                # We try to get the date/group from the config_map (assuming NCAA logic)
                                prev_date = None
                                prev_group = 50
                                
                                if 'config_map' in locals():
                                    p_info = config_map.get(prev_col, {})
                                    prev_date = p_info.get('date')
                                    prev_group = p_info.get('group', 50)
                                elif pool_type == "NFL Survivor":
                                    # If NFL, we assume week numbers (complex to code blindly, but skipping for now)
                                    pass 

                                if prev_date:
                                    # Fetch scores for PREVIOUS round using the same API
                                    df_prev, _, _ = get_sports_data(API_URL, pool_type, prev_date.replace("-", ""), group_id=prev_group)
                                    
                                    if not df_prev.empty:
                                        # Find the game involving the previous pick
                                        # We filter where Team A or Team B matches the pick name
                                        prev_game = df_prev[ (df_prev['Team A'] == prev_pick) | (df_prev['Team B'] == prev_pick) ]
                                        
                                        if not prev_game.empty:
                                            pg_row = prev_game.iloc[0]
                                            p_status = pg_row['Status']
                                            p_winner = pg_row['Winner']

                                            if p_status != "Final":
                                                can_pick = False
                                                block_msg = f"⏳ Your pick for **{prev_col}** ({prev_pick}) is not Final yet. You must wait."
                                            elif p_winner != prev_pick:
                                                can_pick = False
                                                block_msg = f"💀 You picked **{prev_pick}** in {prev_col} and they lost. You are eliminated."
                                        else:
                                            # If game not found (rare), we warn but maybe don't block strictly unless you want to
                                            st.caption(f"⚠️ Could not verify result for {prev_pick}. Proceed with caution.")
                        # --------------------------------------

                        if current_status == "Eliminated":
                            st.error("💀 **You have been Eliminated.**")
                            st.write(f"Your pick for {pick_col}: {user_row.get(pick_col, 'None')}")
                        elif not api_param:
                            st.write("Round not configured.")
                        elif is_locked:
                            st.warning(f"🔒 **Picks Locked for {pick_col}**")
                            st.write(f"Your pick: **{user_row.get(pick_col, 'No Pick')}**")
                        elif not can_pick:
                            # BLOCK THE USER HERE
                            st.error(block_msg)
                            st.write(f"Your pick for {pick_col}: {user_row.get(pick_col, 'None')}")
                        else:
                            # 5. Display Current Pick (Card Style)
                            current_pick_val = str(user_row.get(pick_col, ""))
                            
                            st.markdown("### 👉 Current Pick")
                            if not current_pick_val or current_pick_val == "FALSE":
                                st.info("No Pick Made Yet")
                            else:
                                pick_logo_url = local_logo_map.get(current_pick_val, "")
                                st.markdown(
                                    f"""
                                    <div style="display: flex; align-items: center; background-color: rgba(255, 255, 255, 0.05); padding: 15px; border-radius: 10px; border: 1px solid rgba(255, 255, 255, 0.1);">
                                        <img src="{pick_logo_url}" width="60" style="margin-right: 20px;">
                                        <h1 style="margin: 0; padding: 0; border: none;">{current_pick_val}</h1>
                                    </div>
                                    """, 
                                    unsafe_allow_html=True
                                )
                            st.write("") 
                            
                            # 6. Pick Logic (Filtered Teams)
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
                            
                            if universal_lock and isinstance(universal_lock, datetime):
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

                    # --- AUTO-UPDATING LIVE DASHBOARD ---
                    # We pass the calculated api_param here, but the function also checks session_state
                    render_live_dashboard(
                        sheet_name=TARGET_SHEET_NAME,
                        api_url=API_URL,
                        pool_type=pool_type,
                        api_param=api_param,
                        current_user=st.session_state.current_user,
                        pick_col=pick_col,
                        group_id=group_id_filter,
                        universal_lock_time=universal_lock
                    )
                    st.divider()

    # --- B. ADMIN ACCESS ---
    elif app_mode == "Admin Access":
        with st.sidebar:
            st.divider()
            st.header("🔐 Admin Authorization")
            admin_pass = st.text_input("Admin Password", type="password")
    
        if admin_pass == "admin123":
            st.header(f"🛠️ Admin Dashboard: {pool_type}")
            
            # --- 1. CONFIG & CHECKS ---
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
    
            # --- 2. PAYMENT TRACKER (Kept this new feature for you) ---
            with st.expander("💰 Payment Tracker", expanded=True):
                 if not df.empty and 'Paid' in df.columns:
                    # 1. CALCULATE COUNTS
                    paid_status = df['Paid'].astype(str).str.upper().str.strip()
                    total_players = len(df)
                    paid_count = len(paid_status[paid_status == "TRUE"])
    
                    # 2. ENTRY FEE INPUT & TOTAL POT
                    col_input, _ = st.columns([1, 2])
                    with col_input:
                        entry_fee = st.number_input("Entry Fee ($)", min_value=0, value=25, step=5)
                    
                    total_pot = paid_count * entry_fee
    
                    # 3. DISPLAY METRICS
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Players", total_players)
                    m2.metric("✅ Paid Count", paid_count)
                    m3.metric("💰 Total Pot", f"${total_pot:,.2f}")
                    
                    st.divider()
                    st.write("**Manage Payments**")
                    
                    # 4. THE INTERACTIVE TOGGLE TABLE
                    pay_view = df[['Name', 'Paid']].copy()
                    pay_view['Paid'] = pay_view['Paid'].apply(lambda x: str(x).upper().strip() == 'TRUE')
    
                    edited_pay = st.data_editor(
                        pay_view, 
                        column_config={
                            "Paid": st.column_config.CheckboxColumn("Paid?", default=False),
                            "Name": st.column_config.TextColumn("Player Name", disabled=True)
                        },
                        hide_index=True,
                        use_container_width=True,
                        key="payment_editor"
                    )
    
                    if st.button("💾 Save Payment Changes"):
                        sh = get_google_spreadsheet()
                        ws = sh.worksheet(TARGET_SHEET_NAME)
                        try:
                            paid_col_idx = df.columns.get_loc("Paid") + 1
                            new_values = [['TRUE' if x else 'FALSE'] for x in edited_pay.sort_index()['Paid']]
                            col_letter = gspread.utils.rowcol_to_a1(1, paid_col_idx)[:-1]
                            range_str = f"{col_letter}2:{col_letter}{len(new_values) + 1}"
                            ws.update(range_name=range_str, values=new_values)
                            st.success("✅ Payment status updated!")
                            time.sleep(1)
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error saving payments: {e}")
                 else:
                     st.warning("Payment data not found.")
    
           # This grabs ALL columns that aren't player info (Name, Email, etc.)
            metadata_cols = ["Name", "Email", "Phone", "Security_Hash", "Status", "Paid", "Tiebreaker", "Sort_Key"]
            pick_col_search = [c for c in df.columns if c not in metadata_cols]
                
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
    
            # --- 4. EMAIL LIST ---
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
            
            # --- NEW: MISSING PICKS TRACKER ---
            with st.expander("🕵️ Check Missing Picks (Live)", expanded=False):
                # 1. Load the latest data
                df_admin = load_data(TARGET_SHEET_NAME)
                
                # 2. Get list of Round Columns (exclude info columns)
                meta_cols = ["Name", "Email", "Phone", "Security_Hash", "Status", "Paid", "Tiebreaker", "Sort_Key"]
                round_cols = [c for c in df_admin.columns if c not in meta_cols]
                
                if round_cols:
                    # 3. Dropdown to select the round
                    check_round = st.selectbox("Select Round to Audit:", round_cols)
                    
                    # 4. Filter: Status is NOT Eliminated AND Pick is Empty
                    # We check for Empty String, None, or just whitespace
                    missing_df = df_admin[
                        (df_admin['Status'] != "Eliminated") & 
                        (
                            (df_admin[check_round].isna()) | 
                            (df_admin[check_round] == "") | 
                            (df_admin[check_round].astype(str).str.strip() == "")
                        )
                    ]
                    
                    st.markdown(f"### ⚠️ Missing: **{len(missing_df)}** Players")
                    
                    if not missing_df.empty:
                        # 5. Display Names
                        # Create a nice clean list
                        for player in missing_df['Name'].unique():
                            st.write(f"❌ {player}")
                    else:
                        st.success(f"🎉 Amazing! Every active player has made a pick for {check_round}.")
                else:
                    st.warning("No rounds found in the sheet yet.")
            # ----------------------------------
    
            # --- 5. SPLIT LAYOUT: STANDINGS vs OVERRIDE ---
            st.divider()
            st.subheader("🛠️ Round Manager")
            
            if not pick_col_search:
                st.warning("No Day/Week columns found in the sheet.")
            else:
                col1, col2 = st.columns([2, 1])
            
                # =========================================================
                # STEP 1: RIGHT COLUMN (MASTER CONTROL)
                # We run this side first so the Dropdown updates everything
                # =========================================================
                with col2:
                    # NEW: We use a container with a border instead of a form.
                    # This keeps the "Box" look but allows the dropdown to be interactive.
                    with st.container(border=True):
                        st.subheader("Manual Override")
                        
                        # A. MASTER DROPDOWN (Now inside the box!)
                        target_round = st.selectbox("Column to Edit & Grade", pick_col_search)
                        
                        # B. FETCH DATA (Reactive to the selection above)
                        df_scores = pd.DataFrame()
                        
                        config_map = get_round_config(TARGET_SHEET_NAME)
                        admin_group_id = 50 
                        
                        if target_round in config_map:
                            rd = config_map[target_round]
                            if rd.get('date'): 
                                api_param = rd['date'].replace("-", "")
                                admin_group_id = rd.get('group', 50)
                                df_scores, _, _ = get_sports_data(API_URL, pool_type, api_param, group_id=admin_group_id)
            
                        # C. BUILD SMART TEAM LIST
                        edit_teams = []
                        if not df_scores.empty:
                            edit_teams = sorted(list(set(df_scores['Team A'].tolist() + df_scores['Team B'].tolist())))
                        
                        # D. PLAYER & TEAM SELECTION
                        p_name = st.selectbox("Player", df['Name'].unique() if 'Name' in df.columns else [])
                        
                        if edit_teams:
                            p_team = st.selectbox(f"Select Team", [""] + edit_teams)
                        else:
                            p_team = st.text_input("Set Team (Type Manually)")
                            if not df_scores.empty: st.caption("No teams found for this date.")
            
                        # E. ACTION BUTTON (Standard button now, not form_submit)
                        if st.button("Update Pick & Status", type="primary"):
                            if p_team:
                                sh = get_google_spreadsheet()
                                ws = sh.worksheet(TARGET_SHEET_NAME)
                                try:
                                    cell = ws.find(p_name)
                                    c_idx = ws.find(target_round).col
                                    ws.update_cell(cell.row, c_idx, p_team)
            
                                    # Auto-Update Status for this single player
                                    new_status = "Alive"
                                    if not df_scores.empty:
                                        match = df_scores[df_scores['Team A'].str.contains(p_team, case=False) | 
                                                          df_scores['Team B'].str.contains(p_team, case=False)]
                                        if not match.empty:
                                            game = match.iloc[0]
                                            if game['Status'] == 'Final':
                                                if p_team.lower() not in game['Winner'].lower() and "tie" not in game['Winner'].lower():
                                                    new_status = "Eliminated"
                                                
                                    stat_col_idx = ws.find("Status").col
                                    ws.update_cell(cell.row, stat_col_idx, new_status)
                                    st.success(f"Updated {p_name} -> {p_team}")
                                    st.cache_data.clear(); time.sleep(1.5); st.rerun()
                                except Exception as e: st.error(f"Error: {e}")
                            else: st.warning("Please pick a team.")
            
                # =========================================================
                # STEP 2: LEFT COLUMN (DISPLAY & AUTO-GRADE)
                # Uses the 'target_round' and 'df_scores' from Step 1
                # =========================================================
                with col1:
                    st.subheader(f"Live Standings: {target_round}")
                    
                    # 1. DISPLAY TABLE
                    if not df.empty:
                        # Sort Eliminated to bottom
                        df['Sort_Key'] = df['Status'].apply(lambda x: 1 if x == 'Eliminated' else 0)
                        display_df = df.sort_values(by=['Sort_Key', 'Name']).drop(columns=['Security_Hash', 'Email', 'Sort_Key'], errors='ignore')
                        
                        def color_status(val): return 'color: red; font-weight: bold' if val == "Eliminated" else 'color: green'
                        st.dataframe(display_df.style.applymap(color_status, subset=['Status']), use_container_width=True, height=500)             

                        
                
            
            st.markdown("---")
            st.subheader("📢 Update Global Banner")
            
            # 1. Get current message so you can edit it
            current_banner = get_global_banner()
            
            # 2. Text Input for new message
            new_banner_text = st.text_area(
                "Banner Message (Clear text to hide banner)", 
                value=current_banner if current_banner else ""
            )
            
            # 3. Save Button
            if st.button("Update Banner Message"):
                try:
                    sh = get_google_spreadsheet()
                    ws = sh.worksheet("Settings")
                    
                    # Update cell B1
                    ws.update_acell('B1', new_banner_text)
                    
                    st.success("✅ Banner updated! It will appear for players immediately.")
                    
                    # IMPORTANT: Clear cache so the change shows up right away
                    st.cache_data.clear()
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error updating banner: {e}")
                    
            st.divider()
            st.subheader("📧 Configure Welcome Email")
            
            with st.expander("Edit Email Template", expanded=False):
                st.info("Tip: Use **{name}** in the body to automatically insert the player's name.")
                
                # 1. Fetch current settings
                try:
                    sh = get_google_spreadsheet()
                    ws = sh.worksheet("Settings")
                    curr_sub = ws.acell('B2').value
                    curr_body = ws.acell('B3').value
                except:
                    curr_sub = ""
                    curr_body = ""

                # 2. Input Fields
                new_sub = st.text_input("Email Subject", value=curr_sub if curr_sub else "Welcome to the Pool! 🏀")
                new_body = st.text_area("Email Body", value=curr_body if curr_body else "Hi {name},\n\nWelcome to the pool!", height=200)

                # 3. Save Button
                if st.button("💾 Save Email Settings"):
                    try:
                        sh = get_google_spreadsheet()
                        ws = sh.worksheet("Settings")
                        ws.update_acell('B2', new_sub)
                        ws.update_acell('B3', new_body)
                        st.success("✅ Email template saved!")
                    except Exception as e:
                        st.error(f"Error saving settings: {e}")  
    
        elif admin_pass: st.error("Wrong Password")

if __name__ == '__main__':
    main()
