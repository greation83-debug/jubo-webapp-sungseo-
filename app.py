import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from datetime import datetime
import json

# 페이지 설정
st.set_page_config(
    page_title="성서교회 주보 관리 시스템",
    page_icon="📖",
    layout="wide"
)

# Google Sheets 연결
@st.cache_resource
def get_google_sheets_client():
    """Google Sheets 클라이언트 생성"""
    try:
        # Streamlit secrets에서 인증 정보 가져오기
        credentials_dict = st.secrets["google_sheets"]
        
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        
        credentials = Credentials.from_service_account_info(
            credentials_dict,
            scopes=scopes
        )
        
        return gspread.authorize(credentials)
    except Exception as e:
        st.error(f"Google Sheets 연결 오류: {e}")
        return None

# 데이터 로드
@st.cache_data(ttl=300)  # 5분 캐시
def load_data_from_sheets():
    """Google Sheets에서 데이터 로드"""
    try:
        client = get_google_sheets_client()
        if not client:
            return None
        
        # 시트 열기 (시트 URL 또는 이름)
        sheet_url = st.secrets["sheet_url"]
        spreadsheet = client.open_by_url(sheet_url)
        worksheet = spreadsheet.sheet1
        
        # 데이터 가져오기
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        # 날짜 컬럼 변환
        if '날짜' in df.columns:
            df['날짜'] = pd.to_datetime(df['날짜'], errors='coerce')
        
        return df
    except Exception as e:
        st.error(f"데이터 로드 오류: {e}")
        return None

# Gemini API 설정 - 여러 키 로테이션
def init_gemini():
    """Gemini API 초기화 - 여러 키 순환 사용"""
    try:
        # API 키 리스트 가져오기
        api_keys = st.secrets.get("gemini_api_keys", [])
        
        # 단일 키만 있는 경우 (하위 호환)
        if not api_keys and "gemini_api_key" in st.secrets:
            api_keys = [st.secrets["gemini_api_key"]]
        
        if not api_keys:
            st.error("API 키가 설정되지 않았습니다.")
            return None
        
        # 모델명 (secrets에서 설정 가능, 기본값: gemini-3-flash-preview)
        model_name = st.secrets.get("gemini_model", "gemini-3-flash-preview")
        
        # 순환하며 작동하는 키 찾기
        for i, api_key in enumerate(api_keys):
            try:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(model_name)
                
                # 키가 작동하는지 간단히 테스트
                # (실제 호출 시 오류 나면 다음 키로 전환)
                st.session_state['current_api_key_index'] = i
                st.session_state['api_keys'] = api_keys
                st.session_state['model_name'] = model_name
                
                return model
                
            except Exception as e:
                # 이 키는 실패, 다음 키 시도
                continue
        
        st.error("모든 API 키가 할당량을 초과했습니다. 나중에 다시 시도해주세요.")
        return None
        
    except Exception as e:
        st.error(f"Gemini API 초기화 오류: {e}")
        return None

# Gemini API 호출 (재시도 로직 포함)
def call_gemini_with_retry(prompt, generation_config):
    """여러 API 키로 재시도하며 Gemini 호출"""
    api_keys = st.session_state.get('api_keys', [])
    model_name = st.session_state.get('model_name', 'gemini-3-flash-preview')
    start_index = st.session_state.get('current_api_key_index', 0)
    
    # 모든 키를 순환하며 시도
    for attempt in range(len(api_keys)):
        current_index = (start_index + attempt) % len(api_keys)
        current_key = api_keys[current_index]
        
        try:
            # API 키 설정
            genai.configure(api_key=current_key)
            model = genai.GenerativeModel(model_name)
            
            # 실제 호출
            response = model.generate_content(prompt, generation_config=generation_config)
            
            # 성공! 현재 인덱스 저장
            st.session_state['current_api_key_index'] = current_index
            
            # 디버그 정보 (선택사항)
            if attempt > 0:
                st.info(f"ℹ️ API 키 #{current_index + 1} 사용 중")
            
            return response.text
            
        except Exception as e:
            error_msg = str(e)
            
            # 할당량 초과 오류인 경우 다음 키 시도
            if "quota" in error_msg.lower() or "limit" in error_msg.lower():
                if attempt < len(api_keys) - 1:
                    st.warning(f"⚠️ API 키 #{current_index + 1} 할당량 초과. 다음 키 시도 중...")
                    continue
                else:
                    return "❌ 모든 API 키의 할당량이 초과되었습니다. 내일 다시 시도해주세요."
            else:
                # 다른 오류는 바로 반환
                return f"❌ 오류: {error_msg}"
    
    return "❌ API 호출 실패"

# 이번 주 과거 기록
def get_this_week_history(df, current_date=None):
    """현재 주차의 과거 기록 조회"""
    if current_date is None:
        current_date = datetime.now()
    
    current_week = current_date.isocalendar()[1]
    current_month = current_date.month
    
    history = {}
    
    for year in range(df['날짜'].dt.year.min(), current_date.year):
        year_data = df[
            (df['날짜'].dt.year == year) &
            (
                (df['날짜'].dt.isocalendar().week == current_week) |
                (
                    (df['날짜'].dt.month == current_month) &
                    (df['날짜'].dt.day.between(current_date.day - 7, current_date.day + 7))
                )
            )
        ]
        
        if not year_data.empty:
            history[year] = year_data
    
    return history

# 반복 이벤트 찾기
def find_recurring_events(df, month):
    """특정 월의 반복 이벤트 찾기"""
    month_data = df[df['날짜'].dt.month == month].copy()
    
    if month_data.empty:
        return pd.DataFrame()
    
    event_counts = month_data.groupby('제목').agg({
        '날짜': 'count',
        '카테고리': 'first',
        '내용': 'first'
    }).rename(columns={'날짜': '횟수'})
    
    recurring = event_counts[event_counts['횟수'] >= 3].sort_values('횟수', ascending=False)
    
    return recurring

# 다음 달 광고 추천
def suggest_next_month_ads(df, target_month):
    """Gemini API로 다음 달 광고 추천 (재시도 로직 포함)"""
    month_data = df[df['날짜'].dt.month == target_month]
    recurring = find_recurring_events(df, target_month)
    
    # 데이터 준비
    past_events = month_data[['날짜', '카테고리', '제목', '내용']].head(50).to_dict('records')
    recurring_events = recurring.head(20).to_dict('index')
    
    prompt = f"""다음은 성서교회의 과거 {target_month}월 주보 데이터입니다.

**과거 {target_month}월 주요 이벤트 (최근 50개):**
{json.dumps(past_events, ensure_ascii=False, indent=2)}

**{target_month}월에 3년 이상 반복되는 이벤트:**
{json.dumps(recurring_events, ensure_ascii=False, indent=2)}

이 데이터를 바탕으로 올해 {target_month}월에 필요한 주보 광고를 추천해주세요.

다음 형식으로 답변해주세요:

## {target_month}월 필수 광고 (매년 반복)
1. [광고명] - [추천 게재 주차]

## {target_month}월 권장 광고
1. [광고명] - [추천 게재 주차]

## 특별 고려사항
- [교회 절기나 특별한 날]
"""

    # 새로운 재시도 로직 사용
    return call_gemini_with_retry(
        prompt,
        generation_config=genai.GenerationConfig(
            temperature=0.3,
            max_output_tokens=2000,
        )
    )

# 메인 앱
def main():
    st.title("📖 성서교회 주보 관리 시스템")
    st.markdown("---")
    
    # 사이드바
    with st.sidebar:
        st.header("⚙️ 설정")
        
        # 데이터 새로고침 버튼
        if st.button("🔄 데이터 새로고침"):
            st.cache_data.clear()
            st.rerun()
        
        st.markdown("---")
        st.info("💡 데이터는 Google Sheets에서 자동으로 가져옵니다.")
    
    # 데이터 로드
    with st.spinner("데이터 로드 중..."):
        df = load_data_from_sheets()
    
    if df is None or df.empty:
        st.error("⚠️ 데이터를 불러올 수 없습니다. Google Sheets 설정을 확인해주세요.")
        st.stop()
    
    # 데이터 통계
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("전체 항목", f"{len(df)}개")
    with col2:
        st.metric("기간", f"{df['날짜'].min().year} - {df['날짜'].max().year}")
    with col3:
        st.metric("카테고리", f"{df['카테고리'].nunique()}개")
    with col4:
        st.metric("최근 업데이트", df['날짜'].max().strftime('%Y-%m-%d'))
    
    st.markdown("---")
    
    # 탭 메뉴
    tab1, tab2, tab3, tab4 = st.tabs([
        "📅 이번 주 과거 기록",
        "🔮 다음 달 광고 추천",
        "📊 월별 패턴 분석",
        "🔍 데이터 검색"
    ])
    
    # 탭 1: 이번 주 과거 기록
    with tab1:
        st.header("📅 이번 주 과거 기록")
        st.info("💡 작년 이맘때는 어떤 일이 있었을까요?")
        
        current_date = datetime.now()
        history = get_this_week_history(df, current_date)
        
        if not history:
            st.warning("과거 기록이 없습니다.")
        else:
            for year in sorted(history.keys(), reverse=True):
                with st.expander(f"📅 {year}년 이맘때...", expanded=(year == max(history.keys()))):
                    year_df = history[year]
                    
                    for _, row in year_df.iterrows():
                        st.markdown(f"""
                        **{row['날짜'].strftime('%Y-%m-%d')}** [{row['카테고리']}] **{row['제목']}**
                        
                        {row['내용'] if pd.notna(row['내용']) else ''}
                        """)
                        st.markdown("---")
    
    # 탭 2: 다음 달 광고 추천
    with tab2:
        st.header("🔮 다음 달 광고 추천")
        st.info("💡 Gemini AI가 과거 패턴을 분석하여 추천합니다.")
        
        col1, col2 = st.columns([1, 3])
        
        with col1:
            next_month = (datetime.now().month % 12) + 1
            selected_month = st.selectbox(
                "분석할 월 선택",
                range(1, 13),
                index=next_month - 1
            )
        
        with col2:
            if st.button("✨ AI 추천 생성", type="primary"):
                with st.spinner("Gemini AI가 분석 중입니다..."):
                    # API 초기화 (키 로테이션 준비)
                    init_gemini()
                    
                    # AI 추천 생성
                    suggestion = suggest_next_month_ads(df, selected_month)
                    st.markdown(suggestion)
    
    # 탭 3: 월별 패턴 분석
    with tab3:
        st.header("📊 월별 패턴 분석")
        
        selected_month = st.selectbox(
            "분석할 월 선택",
            range(1, 13),
            format_func=lambda x: f"{x}월"
        )
        
        recurring = find_recurring_events(df, selected_month)
        
        if recurring.empty:
            st.warning(f"{selected_month}월에 반복되는 이벤트가 없습니다.")
        else:
            st.subheader(f"{selected_month}월 반복 이벤트 (3년 이상)")
            
            # 데이터프레임 표시
            display_df = recurring.reset_index()
            display_df.columns = ['제목', '반복 횟수', '카테고리', '내용']
            
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True
            )
            
            # 차트
            st.bar_chart(recurring['횟수'])
    
    # 탭 4: 데이터 검색
    with tab4:
        st.header("🔍 데이터 검색")
        
        col1, col2 = st.columns(2)
        
        with col1:
            search_keyword = st.text_input("키워드 검색", placeholder="예: 양육훈련, 감사예배")
        
        with col2:
            search_category = st.multiselect(
                "카테고리 필터",
                options=df['카테고리'].unique().tolist()
            )
        
        # 검색 실행
        filtered_df = df.copy()
        
        if search_keyword:
            filtered_df = filtered_df[
                filtered_df['제목'].str.contains(search_keyword, case=False, na=False) |
                filtered_df['내용'].str.contains(search_keyword, case=False, na=False)
            ]
        
        if search_category:
            filtered_df = filtered_df[filtered_df['카테고리'].isin(search_category)]
        
        st.subheader(f"검색 결과: {len(filtered_df)}건")
        
        if not filtered_df.empty:
            # 최신순 정렬
            filtered_df = filtered_df.sort_values('날짜', ascending=False)
            
            # 결과 표시
            for _, row in filtered_df.head(50).iterrows():
                with st.expander(f"{row['날짜'].strftime('%Y-%m-%d')} - {row['제목']}"):
                    st.markdown(f"**카테고리**: {row['카테고리']}")
                    st.markdown(f"**내용**: {row['내용'] if pd.notna(row['내용']) else '없음'}")

if __name__ == "__main__":
    main()
