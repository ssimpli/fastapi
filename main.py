{
  "nodes": [
    {
      "parameters": {
        "jsCode": "// 1. Loop를 돌며 처리된 모든 주문 데이터를 가져옵니다.\n// (주의: 이 노드는 Loop 노드의 'Done' 단자에 연결되어야 모든 데이터를 한 번에 가져올 수 있습니다)\n// n8n 버전에 따라 문법이 다를 수 있으나, 일반적으로 Loop 후단에서는 모든 아이템을 참조할 수 있습니다.\n// 만약 데이터가 안 보이면 'Execute Workflow'를 눌러 전체를 실행해봐야 합니다.\n\nconst processedOrders = $(\"5. 데이터 정리 및 파싱\").all().map(item => item.json);\n\n// 2. 차량 데이터 정의 (선생님이 주신 데이터)\nconst vehicles = [\n  { \"차량번호\": \"제주96바7400\", \"유종\": \"휘발유\", \"수송용량\": 140 },\n  { \"차량번호\": \"제주96바7403\", \"유종\": \"휘발유\", \"수송용량\": 120 },\n  { \"차량번호\": \"제주96바7408\", \"유종\": \"휘발유\", \"수송용량\": 150 },\n  { \"차량번호\": \"제주96바7401\", \"유종\": \"등경유\", \"수송용량\": 120 },\n  { \"차량번호\": \"제주96바7402\", \"유종\": \"등경유\", \"수송용량\": 140 },\n  { \"차량번호\": \"제주96바7404\", \"유종\": \"등경유\", \"수송용량\": 140 },\n  { \"차량번호\": \"제주96바7406\", \"유종\": \"등경유\", \"수송용량\": 140 },\n  { \"차량번호\": \"제주96바7407\", \"유종\": \"등경유\", \"수송용량\": 120 }\n];\n\n// 3. API 전송용 최종 데이터 포장\nreturn {\n  json: {\n    orders: processedOrders,\n    vehicles: vehicles\n  }\n};"
      },
      "id": "prepare-payload",
      "name": "6. 배차 데이터 포장 (주문+차량)",
      "type": "n8n-nodes-base.code",
      "typeVersion": 2,
      "position": [
        1820,
        240
      ]
    },
    {
      "parameters": {
        "method": "POST",
        "url": "https://fastapi-production-0921.up.railway.app/optimize",
        "sendBody": true,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json) }}",
        "options": {}
      },
      "id": "http-request",
      "name": "7. 배차 요청 (FastAPI)",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.1,
      "position": [
        2040,
        240
      ]
    }
  ],
  "connections": {
    "6. 배차 데이터 포장 (주문+차량)": {
      "main": [
        [
          {
            "node": "7. 배차 요청 (FastAPI)",
            "type": "main",
            "index": 0
          }
        ]
      ]
    }
  }
}
