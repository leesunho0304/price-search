가격검색 웹 배포용 - 가장 쉬운 버전

구성 파일:
- app.py
- index.html
- requirements.txt
- render.yaml

목표:
Render에 올려서 전 지점에서 URL로 접속하게 만드는 버전입니다.

가장 쉬운 배포 순서:

1. GitHub 가입 / 로그인

2. 새 저장소 만들기
   예: price-search

3. 이 폴더 안의 파일 4개를 GitHub 저장소에 업로드
   - app.py
   - index.html
   - requirements.txt
   - render.yaml

4. Render 가입 / 로그인
   https://render.com

5. New + 클릭
   → Web Service 선택
   → GitHub 저장소 연결
   → price-search 선택

6. 설정
   Name: price-search
   Environment: Python
   Build Command: pip install -r requirements.txt
   Start Command: gunicorn app:app

7. 환경변수 추가
   Key:
   GOOGLE_SERVICE_ACCOUNT_JSON

   Value:
   gpam.json 파일 내용을 메모장으로 열어서 전체 복사 후 붙여넣기

8. Deploy 클릭

9. 배포 완료 후 URL 확인
   예:
   https://price-search.onrender.com

10. 이 URL을 매니저님들께 공유

주의:
- 서비스 계정 JSON은 절대 카톡이나 게시판에 공유하지 마세요.
- Render 환경변수에만 넣으세요.
- 무료 Render는 오래 안 쓰면 잠들 수 있습니다. 처음 접속이 30초 정도 느릴 수 있습니다.
- 웹페이지에서 [동기화] 버튼을 누르면 최신 가격리스트를 다시 읽습니다.


예외상품 설정 기능:
- 상품 카드 하단 버튼 3개 추가
  1. 제조일: 제조일만 적힌 상품 → 기한확인필요 탭
  2. 관리제외: 유통기한 관리 제외 → 비관리대상
  3. 유통기한: 기존처럼 유통기한 계산
- 예: 새로인 파바빈 검색 → [제조일] 클릭

주의:
- Render 무료 서버는 파일 저장이 영구 보장되지 않을 수 있습니다.
- 장기적으로는 예외상품을 구글시트에 저장하는 방식이 더 안정적입니다.


v2 수정:
- 상품 카드 하단에 예외설정 버튼이 실제로 보이도록 index.html 수정
- 버튼: 제조일 / 관리제외 / 유통기한
- 새로인 파바빈 검색 후 [제조일] 버튼을 누르면 기한확인필요로 분류
