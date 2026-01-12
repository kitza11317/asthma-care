import streamlit as st
import pandas as pd
import altair as alt
import qrcode
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from io import BytesIO
from PIL import Image
from datetime import datetime, timedelta
import numpy as np
import warnings

# ปิด Warning
warnings.filterwarnings("ignore")

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Asthma Care Connect", layout="centered", page_icon="🫁")

# ID ของ Google Sheet
SHEET_ID = "1LF9Yi6CXHaiITVCqj9jj1agEdEE9S-37FwnaxNIlAaE"
SHEET_NAME = "asthma_db"

PATIENTS_GID = "0"              
VISITS_GID = "1491996218"       

# Password (ควรย้ายไป st.secrets ในอนาคต)
ADMIN_PASSWORD = "1234"

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def calculate_predicted_pefr(age, height_cm, gender_prefix):
    if not height_cm or height_cm <= 0: return 0
    is_male = True
    prefix = str(gender_prefix).strip()
    if any(x in prefix for x in ['นาง', 'น.ส.', 'หญิง', 'ด.ญ.', 'Miss', 'Mrs.']): is_male = False
    if age < 15:
        return max(-425.5714 + (5.2428 * height_cm), 100)
    else:
        h = height_cm; a = age
        if is_male: pefr_ls = -16.859 + (0.307*a) + (0.141*h) - (0.0018*a**2) - (0.001*a*h)
        else: pefr_ls = -31.355 + (0.162*a) - (0.00084*a**2) + (0.391*h) - (0.00099*h**2) - (0.00072*a*h)
        return pefr_ls * 60

@st.cache_data(ttl=10)
def load_data_fast(gid):
    try:
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
        df = pd.read_csv(url, on_bad_lines='skip')
        if 'hn' in df.columns:
            df['hn'] = df['hn'].astype(str).str.split('.').str[0].str.strip().apply(lambda x: x.zfill(7))
        return df
    except Exception as e: return pd.DataFrame()

def connect_to_gsheet():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    try:
        if "gcp_service_account" in st.secrets:
            creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
        else:
            creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', scope)
        client = gspread.authorize(creds)
        return client.open_by_key(SHEET_ID)
    except: return None

@st.cache_data(ttl=5) 
def load_data_staff(worksheet_name):
    sh = connect_to_gsheet()
    if not sh: return pd.DataFrame()
    try:
        worksheet = sh.worksheet(worksheet_name)
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        if 'hn' in df.columns:
            df['hn'] = df['hn'].astype(str).str.strip().apply(lambda x: x.zfill(7))
        return df
    except: return pd.DataFrame()

def save_to_sheet(worksheet_name, row_data):
    sh = connect_to_gsheet()
    if sh:
        try:
            worksheet = sh.worksheet(worksheet_name)
            worksheet.append_row(row_data)
            load_data_staff.clear()
            load_data_fast.clear()
            return True
        except: return False
    return False

def mask_text(text):
    if not isinstance(text, str): return str(text)
    if len(text) <= 2: return text[0] + "x" * (len(text)-1)
    return text[:2] + "x" * (len(text)-2)

def generate_qr(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf)
    return buf.getvalue()

def check_technique_status(pt_visits_df):
    if pt_visits_df.empty: return "never", 0, None
    reviews = pt_visits_df[pt_visits_df['technique_check'].astype(str).str.contains('ทำ', na=False)].copy()
    if reviews.empty: return "never", 0, None
    reviews['date'] = pd.to_datetime(reviews['date'])
    last_date = reviews['date'].max()
    days_remaining = (last_date + timedelta(days=365) - pd.to_datetime("today").normalize()).days
    if days_remaining < 0: return "overdue", abs(days_remaining), last_date
    else: return "ok", days_remaining, last_date

def plot_pefr_chart(visits_df, reference_pefr):
    data = visits_df.copy()
    data = data[data['pefr'] > 0]
    if data.empty: return alt.Chart(pd.DataFrame({'date':[], 'pefr':[]})).mark_text(text="ไม่มีข้อมูลกราฟ PEFR")
    data['date'] = pd.to_datetime(data['date'])
    ref_val = reference_pefr if reference_pefr > 0 else data['pefr'].max()
    def get_color(val):
        if val >= ref_val * 0.8: return 'green'
        elif val >= ref_val * 0.5: return 'orange'
        else: return 'red'
    data['color'] = data['pefr'].apply(get_color)
    base = alt.Chart(data).encode(
        x=alt.X('date', title='วันที่', axis=alt.Axis(format='%d/%m/%Y')),
        y=alt.Y('pefr', title='PEFR (L/min)', scale=alt.Scale(domain=[0, ref_val + 150])),
        tooltip=[alt.Tooltip('date', format='%d/%m/%Y'), 'pefr']
    )
    line = base.mark_line(color='gray').encode()
    points = base.mark_circle(size=100).encode(color=alt.Color('color', scale=None))
    rule_green = alt.Chart(pd.DataFrame({'y': [ref_val * 0.8]})).mark_rule(color='green', strokeDash=[5, 5]).encode(y='y')
    rule_red = alt.Chart(pd.DataFrame({'y': [ref_val * 0.5]})).mark_rule(color='red', strokeDash=[5, 5]).encode(y='y')
    return (line + points + rule_green + rule_red).properties(height=350).interactive()

def render_dashboard_charts(patients_df, visits_df):
    if patients_df.empty:
        st.warning("ยังไม่มีข้อมูลผู้ป่วย")
        return None, None

    # --- KPI 1: Status Control (Donut Chart) ---
    if not visits_df.empty:
        visits_df = visits_df.copy()
        visits_df['date'] = pd.to_datetime(visits_df['date'])
        
        # เอา Visit ล่าสุดของแต่ละ HN
        latest_visits = visits_df.sort_values('date').groupby('hn').tail(1)
        
        # [แก้ไข] เปลี่ยนไปใช้ control_level ตามที่คุณแจ้ง
        target_col = 'control_level'
        if target_col not in latest_visits.columns:
            # Fallback เผื่อหาไม่เจอ ลองหา control ธรรมดา
            if 'control' in latest_visits.columns: target_col = 'control'
            else: 
                st.error(f"ไม่พบคอลัมน์ {target_col} ใน Google Sheet")
                return None, None

        status_counts = latest_visits[target_col].value_counts().reset_index()
        status_counts.columns = ['status', 'count']
        
        # Color Map
        color_scale = alt.Scale(domain=['Controlled', 'Partly Controlled', 'Uncontrolled'],
                                range=['#28a745', '#ffc107', '#dc3545'])
        
        base = alt.Chart(status_counts).encode(theta=alt.Theta("count", stack=True))
        pie = base.mark_arc(outerRadius=100, innerRadius=60).encode(
            color=alt.Color("status", scale=color_scale, legend=alt.Legend(title="สถานะ")),
            order=alt.Order("count", sort="descending"),
            tooltip=["status", "count"]
        )
        text = base.mark_text(radius=120).encode(
            text=alt.Text("count", format=",.0f"),
            order=alt.Order("count", sort="descending"),
            color=alt.value("black")  
        )
        chart_control = (pie + text).properties(title="สัดส่วนการคุมอาการ (Visit ล่าสุด)")
    else:
        chart_control = alt.Chart(pd.DataFrame({'x':[]})).mark_text(text="ไม่มีข้อมูล Visit")

    # --- KPI 2: Age Distribution (Histogram) ---
    patients_df = patients_df.copy()
    patients_df['dob'] = pd.to_datetime(patients_df['dob'], errors='coerce')
    now = pd.to_datetime('today')
    patients_df['age'] = (now - patients_df['dob']).astype('<m8[Y]')
    
    chart_age = alt.Chart(patients_df).mark_bar().encode(
        x=alt.X("age", bin=alt.Bin(maxbins=10), title="ช่วงอายุ (ปี)"),
        y=alt.Y("count()", title="จำนวนผู้ป่วย"),
        color=alt.value("#4c78a8"),
        tooltip=["count()"]
    ).properties(title="การกระจายตัวของอายุผู้ป่วย")

    return chart_control, chart_age

# ==========================================
# 3. MAIN APP LOGIC
# ==========================================
query_params = st.query_params
target_hn = query_params.get("hn", None)

if target_hn:
    # ------------------------------------------------------------------
    # PATIENT VIEW (Fast Mode, No Login)
    # ------------------------------------------------------------------
    patients_db_fast = load_data_fast(PATIENTS_GID)
    visits_db_fast = load_data_fast(VISITS_GID)
    target_hn = str(target_hn).strip().zfill(7)
    patient = patients_db_fast[patients_db_fast['hn'] == target_hn]
    
    if not patient.empty:
        pt_data = patient.iloc[0]
        masked_name = f"{pt_data['prefix']}{mask_text(pt_data['first_name'])} {mask_text(pt_data['last_name'])}"
        dob = pd.to_datetime(pt_data['dob'])
        age = (datetime.now() - dob).days // 365
        height = pt_data.get('height', 0)
        predicted_pefr = calculate_predicted_pefr(age, height, pt_data['prefix'])
        ref_pefr = predicted_pefr if predicted_pefr > 0 else pt_data['best_pefr']

        c1, c2 = st.columns([1, 4])
        with c1: st.title("🫁")
        with c2:
            st.markdown(f"### HN: {target_hn}")
            st.markdown(f"**ชื่อ-สกุล:** {masked_name}")
            st.caption("🔒 ข้อมูลผู้ป่วย (PDPA)")
        st.divider()

        pt_visits = visits_db_fast[visits_db_fast['hn'] == target_hn].copy()
        tech_status, tech_days, tech_last_date = check_technique_status(pt_visits)
        
        if tech_status == "overdue": st.error(f"⚠️ เตือน: ขาดทบทวนพ่นยา {tech_days} วัน")
        elif tech_status == "ok": st.success(f"✅ เทคนิคพ่นยา: ปกติ (เหลือ {tech_days} วัน)")

        if not pt_visits.empty:
            last_visit = pt_visits.iloc[-1]
            pefr_show = last_visit['pefr'] if last_visit['pefr'] > 0 else "N/A"
            st.info(f"📋 **สถานะล่าสุด ({last_visit['date']})** PEFR: {pefr_show}")
            st.subheader("📈 กราฟแนวโน้ม")
            chart = plot_pefr_chart(pt_visits, ref_pefr)
            st.altair_chart(chart, use_container_width=True)
            with st.expander("ดูประวัติ"): st.dataframe(pt_visits.sort_values(by="date", ascending=False), hide_index=True)
        else: st.warning("ไม่มีประวัติ")
    else: st.error(f"ไม่พบข้อมูล HN: {target_hn}")

else:
    # ------------------------------------------------------------------
    # STAFF VIEW (Login Required)
    # ------------------------------------------------------------------
    st.sidebar.header("🏥 Asthma Clinic")
    if 'logged_in' not in st.session_state: st.session_state.logged_in = False
    if not st.session_state.logged_in:
        st.title("🔐 เข้าสู่ระบบ")
        password = st.text_input("รหัสผ่าน", type="password")
        if st.button("Login"):
            if password == ADMIN_PASSWORD:
                st.session_state.logged_in = True
                st.rerun()
            else: st.error("❌ รหัสผ่านผิด")
        st.stop()

    if st.sidebar.button("🔓 Logout"):
        st.session_state.logged_in = False
        st.rerun()

    st.sidebar.info(f"สถานะ: เจ้าหน้าที่")
    patients_db = load_data_staff("patients")
    visits_db = load_data_staff("visits")
    
    mode = st.sidebar.radio("เมนูหลัก", ["📊 Dashboard ภาพรวม", "🔍 ค้นหา/บันทึกอาการ", "➕ ลงทะเบียนผู้ป่วยใหม่"])

    if mode == "📊 Dashboard ภาพรวม":
        st.title("📊 Dashboard ภาพรวมคลินิก")
        st.caption(f"ข้อมูล ณ วันที่ {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        
        # --- Metrics Calculation ---
        total_pts = len(patients_db)
        
        this_month = datetime.now().strftime('%Y-%m')
        if not visits_db.empty:
            visits_db['date'] = pd.to_datetime(visits_db['date'], errors='coerce')
            visits_clean = visits_db.dropna(subset=['date'])
            this_month_visits = visits_clean[visits_clean['date'].dt.strftime('%Y-%m') == this_month].shape[0]
            
            # Find Uncontrolled
            last_visits = visits_clean.sort_values('date').groupby('hn').tail(1)
            
            # [แก้ไข] เปลี่ยนไปใช้ control_level
            target_col = 'control_level'
            if target_col not in last_visits.columns: 
                 if 'control' in last_visits.columns: target_col = 'control'
            
            if target_col in last_visits.columns:
                uncontrolled_count = last_visits[last_visits[target_col] == 'Uncontrolled'].shape[0]
            else:
                uncontrolled_count = 0
        else:
            this_month_visits = 0
            uncontrolled_count = 0

        # --- Display Metrics ---
        k1, k2, k3 = st.columns(3)
        k1.metric("ผู้ป่วยทั้งหมด", f"{total_pts} คน", border=True)
        k2.metric("Visit เดือนนี้", f"{this_month_visits} ครั้ง", border=True)
        k3.metric("กลุ่มเสี่ยง (Uncontrolled)", f"{uncontrolled_count} คน", delta_color="inverse", delta=f"⚠️ {uncontrolled_count}", border=True)

        st.divider()

        # --- Display Charts ---
        c1, c2 = st.columns([1, 1])
        chart_control, chart_age = render_dashboard_charts(patients_db, visits_db)
        
        with c1:
            if chart_control:
                st.altair_chart(chart_control, use_container_width=True)
                st.info("💡 **สีเขียว (Controlled):** คุมอาการได้ดี\n\n💡 **สีเหลือง (Partly):** มีอาการบ้าง\n\n💡 **สีแดง (Uncontrolled):** อาการกำเริบ")
        
        with c2:
            if chart_age:
                st.altair_chart(chart_age, use_container_width=True)
            
            if not visits_db.empty:
                # Trend Chart
                visits_db['date'] = pd.to_datetime(visits_db['date'], errors='coerce')
                visits_clean = visits_db.dropna(subset=['date'])
                trend_data = visits_clean.set_index('date').resample('M').size().reset_index(name='count')
                
                chart_trend = alt.Chart(trend_data).mark_line(point=True).encode(
                    x=alt.X('date', title='เดือน', axis=alt.Axis(format='%b %Y')),
                    y=alt.Y('count', title='จำนวน Visit')
                ).properties(title="แนวโน้มผู้รับบริการรายเดือน", height=200)
                st.altair_chart(chart_trend, use_container_width=True)

    elif mode == "➕ ลงทะเบียนผู้ป่วยใหม่":
        st.title("➕ ลงทะเบียนผู้ป่วยรายใหม่")
        with st.form("reg"):
            c1, c2 = st.columns(2); r_hn = c1.text_input("HN"); r_p = c2.selectbox("คำนำหน้า", ["นาย", "นาง", "น.ส.", "ด.ช.", "ด.ญ."])
            c3, c4 = st.columns(2); r_f = c3.text_input("ชื่อ"); r_l = c4.text_input("สกุล")
            c5, c6 = st.columns(2); r_d = c5.date_input("วันเกิด", min_value=datetime(1920, 1, 1)); r_h = c6.number_input("สูง (cm)", 50, 250, 160)
            r_b = st.number_input("Best PEFR", 0, 900, 0)
            if st.form_submit_button("ลงทะเบียน"):
                if r_hn and r_f:
                    if str(r_hn).strip().zfill(7) in patients_db['hn'].values: st.error("HN ซ้ำ")
                    else: 
                        if save_to_sheet("patients", [f"'{str(r_hn).strip().zfill(7)}", r_p, r_f, r_l, str(r_d), r_b, r_h]): st.success("สำเร็จ")
                        else: st.error("ไม่สำเร็จ")
                else: st.error("ข้อมูลไม่ครบ")

    else:
        # ------------------------------------------------------------------
        # SEARCH / VISIT RECORD
        # ------------------------------------------------------------------
        hn_list = patients_db['hn'].unique().tolist(); hn_list.sort()
        selected_hn = st.sidebar.selectbox("เลือกผู้ป่วย", hn_list)
        if selected_hn:
            pt_data = patients_db[patients_db['hn'] == selected_hn].iloc[0]
            pt_visits = visits_db[visits_db['hn'] == selected_hn]
            dob = pd.to_datetime(pt_data['dob']); age = (datetime.now() - dob).days // 365
            predicted_pefr = calculate_predicted_pefr(age, pt_data.get('height', 0), pt_data['prefix'])
            st.title(f"{pt_data['prefix']}{pt_data['first_name']} {pt_data['last_name']}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("HN", pt_data['hn']); c2.metric("อายุ", age); c3.metric("สูง", pt_data.get('height', 0)); c4.metric("Std PEFR", int(predicted_pefr))
            
            tech_status, tech_days, _ = check_technique_status(pt_visits)
            if tech_status == "overdue": st.error(f"🚨 ขาดทบทวนพ่นยา {tech_days} วัน")
            elif tech_status == "never": st.error("🚨 ยังไม่เคยสอนพ่นยา")
            else: st.success(f"✅ สอนพ่นยาแล้ว (เหลือ {tech_days} วัน)")
            
            if not pt_visits.empty:
                last_drp = str(pt_visits.sort_values(by="date").iloc[-1]['drp']).strip()
                if last_drp not in ["", "-", "nan"]: st.warning(f"💊 **DRP ล่าสุด:** {last_drp}")

            st.divider(); st.subheader("📈 กราฟ"); chart = plot_pefr_chart(pt_visits, predicted_pefr); st.altair_chart(chart, use_container_width=True)
            with st.expander("ประวัติ"): st.dataframe(pt_visits.sort_values(by="date", ascending=False), use_container_width=True)
            
            st.divider(); st.subheader("📝 บันทึก Visit")
            with st.form("visit"):
                c1, c2 = st.columns(2); v_d = c1.date_input("วันที่", value=datetime.today())
                with c2: v_p = st.number_input("PEFR", 0, 900, step=10); v_no = st.checkbox("ไม่ได้เป่า")
                if predicted_pefr > 0 and v_p > 0: st.caption(f"คิดเป็น {int((v_p/predicted_pefr)*100)}%")
                v_ctrl = st.radio("Control", ["Controlled", "Partly Controlled", "Uncontrolled"], horizontal=True)
                c3, c4 = st.columns(2); v_c = c3.multiselect("Controller", ["Seretide", "Budesonide", "Symbicort"]); v_r = c4.multiselect("Reliever", ["Salbutamol", "Berodual"])
                c5, c6 = st.columns(2); v_a = c5.slider("ความร่วมมือ", 0, 100, 90); v_rel = c5.checkbox("ญาติรับแทน"); v_t = c6.checkbox("สอนเทคนิค")
                v_drp = st.text_area("DRP"); v_adv = st.text_area("Advice"); v_nt = st.text_input("Note"); v_nx = st.date_input("นัดถัดไป")
                
                if st.form_submit_button("บันทึก"):
                    ap, aa, an = (0, 0, f"[ญาติรับแทน] {v_nt}") if v_rel else (v_p, v_a, v_nt)
                    if v_no: ap = 0
                    if save_to_sheet("visits", [selected_hn, str(v_d), ap, v_ctrl, ", ".join(v_c), ", ".join(v_r), aa, v_drp, v_adv, "ทำ" if v_t else "ไม่ทำ", str(v_nx), an]):
                        st.success("สำเร็จ"); st.rerun()
                    else: st.error("ไม่สำเร็จ")
            
            st.divider(); st.subheader("📇 Asthma Card")
            try:
                if st.secrets and "gcp_service_account" in st.secrets: base_url = "https://asthma-care.streamlit.app"
                else: base_url = "http://localhost:8501"
            except: base_url = "http://localhost:8501"
            
            link = f"{base_url}/?hn={selected_hn}"; c_q, c_t = st.columns([1,2]); c_q.image(generate_qr(link), width=150)
            c_t.markdown(f"**{pt_data['first_name']} {pt_data['last_name']}**\n\n**HN:** {selected_hn}\n\nPredicted PEFR: {int(predicted_pefr)}"); c_t.code(link)
