import streamlit as st
import pandas as pd
import gspread
import requests
import time
import re
import hashlib 

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Survivor Pool", layout="wide")

# --- 2. SECURITY FUNCTIONS (HASHING) ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def check_hashes(password, hashed_text):
    if make_hashes(password) == hashed_text:
        return True
    return False

# --- 3. SHARED FUNCTIONS ---
@st.cache_resource
def get_google_spreadsheet():
    # Ensure 'service_account.json' is in the same folder
    gc = gspread.service_account(filename='service_account.json')
    return gc.open("Survivor_Test")

def check_sheet_exists(sheet_name):
    sh = get_google_spreadsheet()
    try:
        sh.worksheet(sheet_name)
        return True
    except:
        return False

# --- IMPROVED CONFIGURATION HANDLING ---
def ensure_config_sheet():
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet("Config")
    except:
        ws = sh.add_worksheet("Config", 10, 2)
        # Initialize A1/B1 headers and A2/B2 default values
        ws.update([['Setting', 'Value'], ['Picks_Revealed', 'False']], 'A1')
    return ws

def get_reveal_status():
    """Reads directly from Cell B2 to avoid search errors."""
    try:
        ws = ensure_config_sheet()
        # We explicitly read cell B2
        val = ws.acell('B2').value
        return str(val).lower() == 'true'
    except:
        return False

def set_reveal_status(new_status):
    """Writes directly to Cell B2."""
    ws = ensure_config_sheet()
    # FIX: Wrap the single value in a list of lists [[value]]
    ws.update([[str(new_status)]], 'B2')
    
    # Update A2 just in case it was deleted
    ws.update([['Picks_Revealed']], 'A2')

@st.cache_data(ttl=5) 
def load_data(sheet_name):
    sh = get_google_spreadsheet()
    try:
        worksheet = sh.worksheet(sheet_name)
        data = worksheet.get_all_records()
        return pd.DataFrame(data)
    except:
        return pd.DataFrame()

# --- DATA FETCHER ---
@st.cache_data(ttl=300)
def get_sports_data(base_url, pool_type, week_num=None):
    target_url = base_url
    if pool_type == "NFL Survivor" and week_num:
        target_url = f"{base_url}?week={week_num}&seasontype=2" 

    try:
        response = requests.get(target_url)
        data = response.json()
        games = []
        if 'events' in data: 
            for event in data['events']:
                short_status = event['status']['type']['description']
                state = event['status']['type']['state']
                competitors = event['competitions'][0]['competitors']
                team_0 = competitors[0]['team']['displayName']
                score_0 = int(competitors[0]['score'])
                team_1 = competitors[1]['team']['displayName']
                score_1 = int(competitors[1]['score'])
                winner = "TBD"
                if state == "post":
                    if score_0 > score_1: winner = team_0
                    elif score_1 > score_0: winner = team_1
                    else: winner = "Tie"
                is_locked = state in ["in", "post"]
                games.append({
                    "Team A": team_0, "Team B": team_1, "Winner": winner,
                    "Status": short_status, "Score": f"{score_0}-{score_1}",
                    "Locked": is_locked
                })
        if games: return pd.DataFrame(games)
    except Exception as e: print(f"API Error: {e}")

    # Fallback / Dummy Data
    if pool_type == "NFL Survivor":
        teams = ["Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills", "Carolina Panthers", 
                 "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys", "Denver Broncos", 
                 "Detroit Lions", "Green Bay Packers", "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars", 
                 "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Miami Dolphins", 
                 "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants", "New York Jets", 
                 "Philadelphia Eagles", "Pittsburgh Steelers", "San Francisco 49ers", "Seattle Seahawks", 
                 "Tampa Bay Buccaneers", "Tennessee Titans", "Washington Commanders"]
    else: teams = ["Duke", "UNC", "Kansas", "Kentucky", "UConn", "Gonzaga"]
    dummy_games = [{"Team A": t, "Team B": "Bye", "Winner": "TBD", "Status": "Scheduled", "Score": "0-0", "Locked": False} for t in teams]
    return pd.DataFrame(dummy_games)

# --- 4. USER FUNCTIONS ---
def register_user(sheet_name, name, email, password):
    sh = get_google_spreadsheet()
    try:
        ws = sh.worksheet(sheet_name)
        existing_data = pd.DataFrame(ws.get_all_records())
        if not existing_data.empty and 'Email' in existing_data.columns:
            existing_emails = existing_data['Email'].astype(str).str.lower().str.strip().values
            if email.lower().strip() in existing_emails: return False, "Email already registered!"
        
        secure_hash = make_hashes(password)
        ws.append_row([name, email, secure_hash, "Alive"])
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
    except Exception as e: return False

# --- 5. APP INTERFACE ---
with st.sidebar:
    st.title("🏈 Survivor App")
    app_mode = st.selectbox("Mode", ["Player Portal", "Admin Access"])
    st.divider()
    pool_type = st.selectbox("Select Pool", ["NFL Survivor", "March Madness (NCAA)"])
    if pool_type == "NFL Survivor":
        TARGET_SHEET_NAME = "NFL"
        API_URL = "http://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    else:
        TARGET_SHEET_NAME = "NCAA"
        API_URL = "http://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"

# ==========================================
# MODE A: PLAYER PORTAL
# ==========================================
if app_mode == "Player Portal":
    st.header(f"Player Portal: {pool_type}")
    
    if 'current_user' not in st.session_state:
        tab1, tab2 = st.tabs(["🔑 Log In", "📝 Register"])
        with tab1:
            email_input = st.text_input("Email Address")
            password_input = st.text_input("Password", type="password")
            if st.button("Log In"):
                st.cache_data.clear()
                df = load_data(TARGET_SHEET_NAME)
                if not df.empty and 'Email' in df.columns:
                    clean_input = email_input.lower().strip()
                    clean_sheet_emails = df['Email'].astype(str).str.lower().str.strip()
                    if clean_input in clean_sheet_emails.values:
                        user_row = df[clean_sheet_emails == clean_input].iloc[0]
                        if check_hashes(password_input, str(user_row['Security_Hash'])):
                            st.session_state.current_user = user_row['Name']
                            st.session_state.current_email = email_input
                            st.rerun()
                        else: st.error("Incorrect Password.")
                    else: st.error("Email not found.")
                else: st.error("Pool is empty.")

        with tab2:
            st.caption("Create a password to secure your picks.")
            with st.form("reg"):
                n = st.text_input("Name")
                e = st.text_input("Email")
                p = st.text_input("Create Password", type="password")
                if st.form_submit_button("Join Pool"):
                    if n and e and p:
                        if register_user(TARGET_SHEET_NAME, n, e, p)[0]: 
                            st.cache_data.clear(); st.success("Registered! Please Log In.")
                        else: st.error("Registration failed.")
                    else: st.warning("Please fill all fields.")
                        
    # LOGGED IN VIEW
    else:
        col_a, col_b = st.columns([3,1])
        with col_a: st.subheader(f"👋 {st.session_state.current_user}")
        with col_b: 
            if st.button("Log Out"): del st.session_state['current_user']; st.rerun()
        st.divider()

        df = load_data(TARGET_SHEET_NAME)
        
        if df.empty:
            st.warning("Loading data...")
        else:
            pick_cols = [c for c in df.columns if "Week" in c or "Round" in c]
            if not pick_cols: pick_cols = ["Week 1"] 
            
            # WEEK SELECTION (Controls Pick and Breakdown)
            selected_week = st.selectbox("Select Week / Round", pick_cols)
            week_num = re.search(r'\d+', selected_week).group() if re.search(r'\d+', selected_week) else None
            df_scores = get_sports_data(API_URL, pool_type, week_num)

            user_row = df[df['Name'] == st.session_state.current_user].iloc[0]
            past_picks = [str(user_row[c]) for c in pick_cols if c != selected_week and str(user_row[c])]
            
            teams_playing = df_scores[['Team A', 'Team B', 'Locked', 'Status']].to_dict('records')
            available_teams = []
            
            for game in teams_playing:
                if not game['Locked'] and game['Team A'] not in past_picks: available_teams.append(game['Team A'])
                if not game['Locked'] and game['Team B'] not in past_picks and game['Team B'] != "Bye": available_teams.append(game['Team B'])

            # --- PICK SECTION ---
            st.info(f"📅 Making Pick for: **{selected_week}**")
            with st.form("make_pick_form"):
                pick_selection = st.selectbox("Select Team", [""] + sorted(list(set(available_teams))))
                if st.form_submit_button("🔒 Lock In Pick"):
                    if pick_selection:
                        if save_pick_to_sheet(TARGET_SHEET_NAME, st.session_state.current_user, selected_week, pick_selection):
                            st.success(f"Success! You picked {pick_selection}.")
                            st.cache_data.clear()
                            time.sleep(1)
                            st.rerun()
                        else: st.error("Error saving.")
                    else: st.warning("Pick a team.")

            st.divider()
            
            # --- CHECK VISIBILITY SETTING ---
            picks_revealed = get_reveal_status()

            if picks_revealed:
                col1, col2 = st.columns([1, 2])
                with col1:
                    st.subheader(f"📊 Breakdown: {selected_week}")
                    if 'Status' in df.columns:
                        alive_df = df[df['Status'] == 'Alive']
                        if not alive_df.empty:
                            counts = alive_df[selected_week].value_counts().reset_index()
                            counts.columns = ['Team', 'Count']
                            counts = counts[counts['Team'] != ""]
                            st.dataframe(counts, hide_index=True, use_container_width=True)
                        else: st.write("No Alive players.")
                
                with col2:
                    st.subheader("🏆 Live Standings")
                    display_df = df.drop(columns=['Security_Hash', 'Email', 'Login_Code'], errors='ignore')
                    
                    if not df_scores.empty:
                        statuses = []
                        for _, row in df.iterrows():
                            pick = str(row.get(selected_week, "")).strip()
                            match = df_scores[df_scores['Team A'].str.contains(pick, case=False) | df_scores['Team B'].str.contains(pick, case=False)]
                            if match.empty: statuses.append("Unknown")
                            else:
                                game = match.iloc[0]
                                if game['Status'] == 'Final':
                                    if pick.lower() in game['Winner'].lower(): statuses.append("SAFE")
                                    elif game['Winner'] == "Tie": statuses.append("TIE")
                                    else: statuses.append("ELIMINATED")
                                else: statuses.append(f"Pending")
                        display_df['Calculated_Status'] = statuses

                    if 'Calculated_Status' in display_df.columns:
                        def color(val): return 'color: green' if 'SAFE' in str(val) else 'color: red' if 'ELIMINATED' in str(val) else ''
                        st.dataframe(display_df.style.map(color, subset=['Calculated_Status']), use_container_width=True)
                    else:
                        st.dataframe(display_df, use_container_width=True)
            else:
                st.warning("🔒 **Picks Hidden**")
                st.info("The Admin has hidden the picks board. It will open after the games begin!")

# ==========================================
# MODE B: ADMIN ACCESS
# ==========================================
elif app_mode == "Admin Access":
    with st.sidebar:
        st.divider()
        st.header("🔐 Admin Settings")
        admin_pass = st.text_input("Admin Password", type="password")
        if st.button("🔄 Force Reload"): st.cache_data.clear(); st.rerun()

    if admin_pass == "admin123":
        st.header(f"🛠️ Admin Dashboard: {pool_type}")
        
        # --- FIXED: VISIBILITY TOGGLE (Robust) ---
        st.subheader("⚙️ Game Controls")
        
        # Get Current Status
        is_revealed = get_reveal_status()
        
        col_t1, col_t2 = st.columns([1, 4])
        with col_t1:
            if is_revealed:
                if st.button("🔒 Hide Picks"):
                    with st.spinner("Hiding..."):
                        set_reveal_status("False")
                        st.cache_data.clear()
                        time.sleep(1) # Give API time to breathe
                        st.rerun()
            else:
                if st.button("🔓 Reveal Picks"):
                    with st.spinner("Revealing..."):
                        set_reveal_status("True")
                        st.cache_data.clear()
                        time.sleep(1) # Give API time to breathe
                        st.rerun()
        with col_t2:
            if is_revealed: st.success("Status: **VISIBLE**")
            else: st.error("Status: **HIDDEN**")
        
        st.divider()

        if not check_sheet_exists(TARGET_SHEET_NAME):
            if st.button(f"➕ Create '{TARGET_SHEET_NAME}' Tab"):
                sh = get_google_spreadsheet()
                ws = sh.add_worksheet(title=TARGET_SHEET_NAME, rows=100, cols=25)
                headers = ["Name", "Email", "Security_Hash", "Status"] + [f"Week {i}" for i in range(1,19)]
                ws.update('A1', [headers])
                st.rerun()
        else:
            df = load_data(TARGET_SHEET_NAME)
            df_scores = get_sports_data(API_URL, pool_type)

            # --- GOOGLE GROUP HELPER (Active Players Only) ---
            with st.expander("📢 Google Group Helper (Active Players Only)", expanded=True):
                # Filter for ALIVE players only
                if 'Status' in df.columns:
                    alive_only = df[df['Status'] == 'Alive']
                    emails = [e for e in alive_only['Email'].unique() if e and "@" in str(e)]
                    
                    st.markdown(f"**Found {len(emails)} Active Players**")
                    st.text_area("Copy List:", value=", ".join(emails), height=70)
                else:
                    st.warning("No 'Status' column found in sheet.")
            
            # --- LIVE STANDINGS (ADMIN) ---
            col1, col2 = st.columns([2, 1])
            with col1:
                st.subheader("Live Standings (Admin View)")
                display_df = df.drop(columns=['Security_Hash'], errors='ignore')
                pick_col = next((c for c in df.columns if "Week" in c or "Round" in c), None)
                
                if pick_col and not df.empty and not df_scores.empty:
                    statuses = []
                    for _, row in df.iterrows():
                        pick = str(row.get(pick_col, "")).strip()
                        match = df_scores[df_scores['Team A'].str.contains(pick, case=False) | df_scores['Team B'].str.contains(pick, case=False)]
                        if match.empty: statuses.append("Unknown")
                        else:
                            game = match.iloc[0]
                            if game['Status'] == 'Final':
                                if pick.lower() in game['Winner'].lower(): statuses.append("SAFE")
                                elif game['Winner'] == "Tie": statuses.append("TIE")
                                else: statuses.append("ELIMINATED")
                            else: statuses.append(f"Pending")
                    display_df['Calculated_Status'] = statuses
                
                if 'Calculated_Status' in display_df.columns:
                    def color(val): 
                        if 'SAFE' in str(val): return 'color: green' 
                        elif 'ELIMINATED' in str(val): return 'color: red'
                        else: return ''
                    st.dataframe(display_df.style.map(color, subset=['Calculated_Status']), use_container_width=True)
                else:
                    st.dataframe(display_df, use_container_width=True)

            with col2:
                st.subheader("Force Update Pick")
                with st.form("admin_pick"):
                    p_name = st.selectbox("Player", df['Name'].unique() if 'Name' in df.columns else [])
                    cols = [c for c in df.columns if "Week" in c or "Round" in c]
                    p_week = st.selectbox("Column", cols if cols else ["No Cols Found"])
                    teams = sorted(list(set(df_scores['Team A'].tolist() + df_scores['Team B'].tolist()))) if not df_scores.empty else ["No Games"]
                    p_team = st.selectbox("Team", teams)
                    if st.form_submit_button("Update"):
                        sh = get_google_spreadsheet()
                        ws = sh.worksheet(TARGET_SHEET_NAME)
                        try:
                            cell = ws.find(p_name)
                            c_idx = ws.find(p_week).col
                            ws.update_cell(cell.row, c_idx, p_team)
                            st.success("Updated!")
                            time.sleep(1)
                            st.rerun()
                        except: st.error("Error finding cell.")
    elif admin_pass: st.error("Wrong Password")
# import streamlit as st
# import pandas as pd
# import gspread
# import requests
# import smtplib
# from email.message import EmailMessage
# import time
# import mimetypes

# # --- 1. CONFIGURATION ---
# st.set_page_config(page_title="Survivor Pool Manager", layout="wide")
# st.title("🏆 Survivor Pool Manager")

# # --- 2. SIDEBAR: SETTINGS ---
# with st.sidebar:
#     st.header("⚙️ Settings")

#     # A. POOL SELECTOR
#     pool_type = st.selectbox("Select Active Pool", ["NFL Survivor", "March Madness (NCAA)"])
    
#     # Define Target Sheet & API based on selection
#     if pool_type == "NFL Survivor":
#         TARGET_SHEET_NAME = "NFL"
#         API_URL = "http://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
#     else:
#         TARGET_SHEET_NAME = "NCAA"
#         API_URL = "http://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"

#     # B. RESET BUTTON (Forces a refresh)
#     if st.button("🔄 Reload Data"):
#         st.cache_data.clear()
#         st.rerun()

#     st.divider()
    
#     # C. EMAIL SETTINGS
#     st.info("Email Configuration")
#     provider = st.selectbox("Email Provider", ["Gmail", "Outlook / Hotmail", "Yahoo", "iCloud", "Other"])
    
#     if provider == "Gmail": default_server, default_port = "smtp.gmail.com", 465
#     elif provider == "Outlook / Hotmail": default_server, default_port = "smtp.office365.com", 587
#     elif provider == "Yahoo": default_server, default_port = "smtp.mail.yahoo.com", 465
#     elif provider == "iCloud": default_server, default_port = "smtp.mail.me.com", 587
#     else: default_server, default_port = "", 587

#     smtp_server = st.text_input("SMTP Server", value=default_server, disabled=(provider != "Other"))
#     smtp_port = st.number_input("SMTP Port", value=default_port, disabled=(provider != "Other"))

#     if 'sender_email' not in st.session_state: st.session_state.sender_email = ""
#     if 'app_password' not in st.session_state: st.session_state.app_password = ""
    
#     st.session_state.sender_email = st.text_input("Your Email", value=st.session_state.sender_email)
#     st.session_state.app_password = st.text_input("App Password", type="password", value=st.session_state.app_password)

# # --- 3. DATA CONNECTIONS ---

# @st.cache_resource
# def get_google_spreadsheet():
#     # Connect to Google Sheets
#     gc = gspread.service_account(filename='service_account.json')
#     return gc.open("Survivor_Test")

# def check_sheet_exists(sheet_name):
#     # Returns True if the tab exists, False if not
#     sh = get_google_spreadsheet()
#     try:
#         sh.worksheet(sheet_name)
#         return True
#     except:
#         return False

# def load_data(sheet_name):
#     sh = get_google_spreadsheet()
#     try:
#         worksheet = sh.worksheet(sheet_name)
#         data = worksheet.get_all_records()
#         return pd.DataFrame(data)
#     except:
#         return pd.DataFrame()

# @st.cache_data(ttl=300)
# def get_sports_data(url):
#     try:
#         response = requests.get(url)
#         data = response.json()
#         games = []
#         if 'events' not in data: return pd.DataFrame()
#         for event in data['events']:
#             status = event['status']['type']['name']
#             short_status = event['status']['type']['description']
#             competitors = event['competitions'][0]['competitors']
#             team_0 = competitors[0]['team']['displayName']
#             score_0 = int(competitors[0]['score'])
#             team_1 = competitors[1]['team']['displayName']
#             score_1 = int(competitors[1]['score'])
#             winner = "TBD"
#             if status == "STATUS_FINAL":
#                 if score_0 > score_1: winner = team_0
#                 elif score_1 > score_0: winner = team_1
#                 else: winner = "Tie"
#             games.append({
#                 "Team A": team_0, "Team B": team_1, "Winner": winner,
#                 "Status": short_status, "Score": f"{score_0}-{score_1}"
#             })
#         return pd.DataFrame(games)
#     except: return pd.DataFrame()

# # --- 4. EMAIL LOGIC ---
# def send_email_universal(host, port, user, password, recipient_list, subject, html_content, attachment=None):
#     try:
#         msg = EmailMessage()
#         msg['Subject'] = subject
#         msg['From'] = user
#         if isinstance(recipient_list, str): msg['To'] = recipient_list
#         else: msg['To'] = user; msg['Bcc'] = ", ".join(recipient_list)
#         msg.add_alternative(html_content, subtype='html')

#         if attachment is not None:
#             ctype, encoding = mimetypes.guess_type(attachment.name)
#             if ctype is None or encoding is not None: ctype = 'application/octet-stream'
#             maintype, subtype = ctype.split('/', 1)
#             msg.add_attachment(attachment.getvalue(), maintype=maintype, subtype=subtype, filename=attachment.name)

#         if port == 465:
#             with smtplib.SMTP_SSL(host, port) as smtp:
#                 smtp.login(user, password)
#                 smtp.send_message(msg)
#         else:
#             with smtplib.SMTP(host, port) as smtp:
#                 smtp.starttls(); smtp.login(user, password); smtp.send_message(msg)
#         return True
#     except Exception as e:
#         st.error(f"Email Error: {e}")
#         return False

# # --- 5. MAIN APP LOGIC ---

# # Step A: Check if the Tab Exists
# sheet_exists = check_sheet_exists(TARGET_SHEET_NAME)

# if not sheet_exists:
#     # --- SCENARIO 1: Tab Missing ---
#     st.warning(f"⚠️ The tab '{TARGET_SHEET_NAME}' was not found in your Google Sheet.")
    
#     if st.button(f"➕ Create '{TARGET_SHEET_NAME}' Tab"):
#         sh = get_google_spreadsheet()
#         try:
#             ws = sh.add_worksheet(title=TARGET_SHEET_NAME, rows=100, cols=20)
#             headers = ["Name", "Email", "Status"]
#             if "NCAA" in TARGET_SHEET_NAME: headers += ["Round 1", "Round 2", "Round 3"]
#             else: headers += ["Week 1", "Week 2", "Week 3"]
#             ws.update('A1', [headers])
#             st.success("Created! Reloading...")
#             time.sleep(1)
#             st.rerun()
#         except Exception as e:
#             st.error(f"Error: {e}")

# else:
#     # --- SCENARIO 2: Tab Exists (Load Dashboard) ---
#     df = load_data(TARGET_SHEET_NAME)
    
#     # Handle Empty Sheet (Just headers)
#     if df.empty:
#         st.info(f"The '{TARGET_SHEET_NAME}' pool is currently empty. Add players in Google Sheets or use the Admin panel below.")
#         # Create a dummy dataframe so the app doesn't crash
#         df = pd.DataFrame(columns=["Name", "Email", "Status", "Week 1"] if pool_type=="NFL Survivor" else ["Name", "Email", "Status", "Round 1"])

#     # Merge Scores Logic
#     df_scores = get_sports_data(API_URL)
#     pick_col = next((c for c in df.columns if "Week" in c or "Round" in c), None)

#     if pick_col and not df.empty and not df_scores.empty:
#         statuses, scores_display = [], []
#         for index, row in df.iterrows():
#             pick = str(row.get(pick_col, "")).strip()
#             match = df_scores[df_scores['Team A'].str.contains(pick, case=False) | df_scores['Team B'].str.contains(pick, case=False)]
#             if match.empty:
#                 statuses.append("Unknown / Bye")
#                 scores_display.append("-")
#             else:
#                 game = match.iloc[0]
#                 scores_display.append(f"{game['Team A']} {game['Score']} {game['Team B']}")
#                 if game['Status'] == 'Final':
#                     if pick.lower() in game['Winner'].lower(): statuses.append("SAFE")
#                     elif game['Winner'] == "Tie": statuses.append("TIE")
#                     else: statuses.append("ELIMINATED")
#                 else: statuses.append(f"Pending ({game['Status']})")
#         df['Calculated_Status'] = statuses
#         df['Game Score'] = scores_display

#     # --- RENDER DASHBOARD ---
#     st.subheader(f"Viewing: {pool_type}")
    
#     # Admin Email
#     with st.expander("📢 Admin Email Blast", expanded=False):
#         subj = st.text_input("Subject", value=f"{pool_type} Update")
#         msg_body = st.text_area("Message")
#         attach = st.file_uploader("Attachment")
#         recipients = [e for e in df['Email'].unique() if e and "@" in str(e)] if 'Email' in df.columns else []
#         if st.button("Send Email"):
#             if not st.session_state.sender_email: st.error("Check Sidebar Settings")
#             elif send_email_universal(smtp_server, smtp_port, st.session_state.sender_email, st.session_state.app_password, recipients, subj, f"<html><body>{msg_body}</body></html>", attach):
#                 st.success("Sent!")

#     col1, col2 = st.columns([2, 1])
#     with col1:
#         st.caption("Live Standings")
#         if 'Calculated_Status' in df.columns:
#             def color(val):
#                 return 'color: green; font-weight: bold' if 'SAFE' in str(val) else 'color: red; font-weight: bold' if 'ELIMINATED' in str(val) else ''
#             st.dataframe(df.style.map(color, subset=['Calculated_Status']), use_container_width=True)
#         else:
#             st.dataframe(df, use_container_width=True)

#     with col2:
#         st.caption("Make/Edit Picks")
#         with st.form("pick_form"):
#             name = st.selectbox("Player", df['Name'].unique() if 'Name' in df.columns else [])
#             # Fix: Ensure we have columns to select
#             time_cols = [c for c in df.columns if "Week" in c or "Round" in c]
#             if not time_cols: time_cols = ["Week 1"] # Fallback
#             week = st.selectbox("Timeframe", time_cols)
            
#             # Teams
#             teams = sorted(list(set(df_scores['Team A'].tolist() + df_scores['Team B'].tolist()))) if not df_scores.empty else ["No Games Found"]
#             pick = st.selectbox("Team", teams)
            
#             if st.form_submit_button("Save Pick"):
#                 sh = get_google_spreadsheet()
#                 ws = sh.worksheet(TARGET_SHEET_NAME)
#                 try:
#                     cell = ws.find(name)
#                     col_idx = ws.find(week).col
#                     ws.update_cell(cell.row, col_idx, pick)
#                     st.success("Updated!")
#                     time.sleep(1)
#                     st.rerun()
#                 except:
#                     st.error("Player or Column not found in Sheet.")

# # --- 6. DEBUGGER (Hidden at bottom of sidebar) ---
# with st.sidebar:
#     st.divider()
#     with st.expander("🛠️ Debugger"):
#         sh = get_google_spreadsheet()
#         all_sheets = [s.title for s in sh.worksheets()]
#         st.write(f"Looking for: **{TARGET_SHEET_NAME}**")
#         st.write(f"Found Sheets: {all_sheets}")
#         st.write(f"Connection Status: {'✅ Found' if sheet_exists else '❌ Missing'}")