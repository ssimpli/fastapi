---
title: FastAPI
description: A FastAPI server
tags:
  - fastapi
  - hypercorn
  - python
---

# FastAPI Example

This example starts up a [FastAPI](https://fastapi.tiangolo.com/) server.

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/-NvLj4?referralCode=CRJ8FE)
## ✨ Features

- FastAPI
- [Hypercorn](https://hypercorn.readthedocs.io/)
- Python 3

## 💁‍♀️ How to use

- Clone locally and install packages with pip using `pip install -r requirements.txt`
- Run locally using `hypercorn main:app --reload`

## 🧪 Testing

- `python -m compileall main.py` — precompiles the FastAPI entrypoint to bytecode to catch syntax errors early without running the server. This lightweight check is useful in CI or before deploying to Railway.

## 🧭 빨간색/초록색(diff) 수정 적용하기

코드 리뷰나 가이드에 제시되는 빨간색(삭제)과 초록색(추가) 줄은 Git diff 형식입니다. 로컬에서 그대로 적용하려면 아래 순서로 진행하세요.

1. 원하는 diff 내용을 파일로 저장합니다. (예: `change.patch`)
2. 저장한 위치에서 `git apply change.patch`를 실행합니다. 이미 적용된 줄이 있을 경우 `git apply --reject change.patch`로 충돌 난 부분을 별도 `.rej` 파일로 확인하고 수동 반영할 수 있습니다.
3. `git status`로 수정 내역을 확인한 뒤 필요하면 추가 커밋을 만듭니다.

단순히 일부 줄만 반영하고 싶다면 에디터에서 해당 파일을 열어 초록색 추가 줄을 삽입하고 빨간색 삭제 줄을 제거하는 방식으로 수동 편집해도 됩니다.

### 🤔 어디서 git 명령을 실행하나요?

- **로컬 PC 터미널**: macOS의 터미널, Windows PowerShell/Git Bash에서 `git clone …`, `git apply …`처럼 명령을 그대로 입력합니다.
- **브라우저 기반 IDE**: GitHub Codespaces, Railway Shell, Replit 등 브라우저에서 제공하는 터미널 창을 열어 동일한 git 명령을 실행할 수 있습니다.
- **도커/개발 컨테이너**: 리포지토리를 컨테이너로 띄웠다면 `docker exec -it <컨테이너>`로 셸에 들어가 위 명령을 실행합니다.

즉, 초록색/빨간색 diff는 “어디에서든 터미널을 열고 git이 설치된 환경”이면 적용할 수 있으며, 크롬에서 보고 있는 경우에도 터미널 탭(또는 클라우드 IDE의 셸)을 열어 동일한 명령을 입력하면 됩니다.

### ❓ README를 통째로 복사해서 GitHub에 붙여넣어야 하나요?

- 아닙니다. 리포지토리를 **클론하거나 Codespaces/웹 IDE에서 열어** README를 직접 편집하면 됩니다.
- GitHub 웹 UI에서 바로 수정하려면 `README.md` 파일을 열고 **연필 아이콘(Edit this file)**을 눌러 필요한 부분만 고친 뒤 커밋을 생성하세요. 전체 내용을 복사·붙여넣을 필요는 없습니다.
- 터미널이 있는 환경이라면 위의 git 명령 실행 안내에 따라 `git apply`나 에디터를 사용해 변경분을 반영하고 커밋하면 됩니다.

## 📝 Notes

- To learn about how to use FastAPI with most of its features, you can visit the [FastAPI Documentation](https://fastapi.tiangolo.com/tutorial/)
- To learn about Hypercorn and how to configure it, read their [Documentation](https://hypercorn.readthedocs.io/)
