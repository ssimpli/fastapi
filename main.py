import json
import os
import requests  # URL 데이터 다운로드용
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# OR-Tools 라이브러리 (Railway 배포 시 설치됨)
try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    print("❌ OR-Tools가 설치되지 않았습니다. requirements.txt를 확인하세요.")

app = FastAPI()

# ==========================================
# 1. 데이터 모델 정의 (n8n에서 보낼 데이터와 일치)
# ==========================================

class OrderItem(BaseModel):
    주유소명: str
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    start_min: int = 540  # 기본값 09:00
    end_min: int = 1080   # 기본값 18:00
    priority: int = 2     # 1:긴급, 2:보통, 3:여유

class VehicleItem(BaseModel):
    차량번호: str
    유종: str  # "휘발유" 또는 "등경유"
    수송용량: int

class OptimizationRequest(BaseModel):
    orders: List[OrderItem]
    vehicles: List[VehicleItem]

# ==========================================
# 2. 거리 매트릭스 데이터 로드 (서버 시작 시 1회)
# ==========================================
# 전역 변수에 매트릭스 데이터를 담아둡니다.
MATRIX_DATA = {}

def load_matrix_data():
    global MATRIX_DATA
    raw_matrix = None
    
    # [방법 1] 환경변수(JEJU_MATRIX_URL)에 있는 Gist 주소에서 다운로드 (추천)
    matrix_url = os.environ.get("JEJU_MATRIX_URL")
    
    if matrix_url:
        try:
            print(f"🌐 URL에서 매트릭스 다운로드 시도... ({matrix_url[:40]}...)")
            # 타임아웃 15초 설정
            response = requests.get(matrix_url, timeout=15)
            
            if response.status_code == 200:
                raw_matrix = response.json()
                print("✅ URL 로드 성공!")
            else:
                print(f"❌ URL 로드 실패: 상태 코드 {response.status_code}")
        except Exception as e:
            print(f"❌ URL 다운로드 중 에러 발생: {e}")

    # [방법 2] 로컬 파일 (URL 실패 시 백업용)
    if not raw_matrix and os.path.exists("jeju_distance_matrix_full.json"):
        try:
            print("📂 로컬 파일에서 매트릭스 로드 중...")
            with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
                raw_matrix = json.load(f)
            print("✅ 로컬 파일 로드 성공")
        except Exception as e:
            print(f"❌ 로컬 파일 로드 실패: {e}")

    # 데이터 적용 (구조 확인)
    if raw_matrix:
        # JSON 파일 구조가 { "matrix": { ... } } 인 경우
        if "matrix" in raw_matrix and isinstance(raw_matrix["matrix"], dict):
            MATRIX_DATA = raw_matrix["matrix"]
        # JSON 파일 자체가 매트릭스인 경우
        else:
            MATRIX_DATA = raw_matrix
        print(f"📊 로드된 전체 노드 수: {len(MATRIX_DATA)}")
    else:
        print("⚠️ [경고] 매트릭스 데이터를 가져오지 못했습니다. 거리 계산 시 기본값(30분)이 사용됩니다.")

# 서버 시작 시 바로 실행
load_matrix_data()


# ==========================================
# 3. VRP 알고리즘 핵심 로직
# ==========================================

def solve_vrp_algorithm(orders, vehicles, fuel_type_filter):
    # (1) 유종에 맞는 주문과 차량만 걸러내기
    target_orders = []
    for order in orders:
        # 휘발유 차량이면 '휘발유' 주문량, 등경유 차량이면 '등유+경유' 합산량
        amount = order.휘발유 if fuel_type_filter == "휘발유" else (order.등유 + order.경유)
        if amount > 0:
            target_orders.append(order)
            
    target_vehicles = [v for v in vehicles if v.유종 == fuel_type_filter]
    
    # 데이터가 없으면 빈 결과 반환
    if not target_orders or not target_vehicles:
        return {"status": "skipped", "reason": f"{fuel_type_filter} 데이터 없음", "routes": []}

    # (2) 방문지 리스트 생성 (0번 인덱스는 항상 물류센터)
    depot_name = "제주물류센터"
    location_names = [depot_name] + [o.주유소명 for o in target_orders]
    num_locations = len(location_names)
    
    # (3) 거리/시간 매트릭스 구성 (필요한 부분만 추출)
    # OR-Tools는 정수형(Integer) 매트릭스만 받습니다.
    time_matrix = [[0] * num_locations for _ in range(num_locations)]
    
    try:
        for i in range(num_locations):
            for j in range(num_locations):
                if i == j: continue # 자기 자신으로 가는 거리는 0
                
                origin = location_names[i]
                dest = location_names[j]
                
                # 전역 변수 MATRIX_DATA에서 조회
                if origin in MATRIX_DATA and dest in MATRIX_DATA[origin]:
                    # JSON의 숫자가 실수(float)일 수 있으므로 int로 변환
                    val = MATRIX_DATA[origin][dest]
                    time_matrix[i][j] = int(float(val))
                else:
                    # 데이터가 없으면 기본값 30분 가정 (에러 방지)
                    time_matrix[i][j] = 30 
    except Exception as e:
        print(f"Matrix 구성 중 에러: {e}")
        return {"status": "error", "message": str(e)}
    
    # (4) OR-Tools 데이터 모델 생성
    
    # 4-1. 각 지점별 수요량 (Demand)
    demands = [0] # 0번(센터)은 수요 없음
    for o in target_orders:
        amt = o.휘발유 if fuel_type_filter == "휘발유" else (o.등유 + o.경유)
        demands.append(amt)
        
    # 4-2. 차량별 용량 (Capacity)
    vehicle_capacities = [v.수송용량 for v in target_vehicles]
    num_vehicles = len(target_vehicles)
    
    # 4-3. 시간 창 (Time Windows)
    time_windows = [(0, 1440)] # 0번(센터)은 24시간 열려있음
    for o in target_orders:
        time_windows.append((o.start_min, o.end_min))

    # (5) Solver 인스턴스 생성 및 설정
    manager = pywrapcp.RoutingIndexManager(num_locations, num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    # 콜백: 비용(시간) 계산
    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return time_matrix[from_node][to_node]
    
    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # 콜백: 용량(수요) 계산
    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return demands[from_node]
    
    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    
    # 제약조건 추가: 차량 용량
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index,
        0,  # null capacity slack
        vehicle_capacities, # 차량별 용량 배열
        True, # start cumul to zero
        'Capacity'
    )

    # 제약조건 추가: 시간 (Time Windows)
    routing.AddDimension(
        transit_callback_index,
        120,  # 대기 허용 시간 (일찍 도착 시 최대 120분 대기 가능)
        1440, # 차량의 하루 최대 운행 시간 (24시간)
        False, 
        'Time'
    )
    time_dimension = routing.GetDimensionOrDie('Time')
    
    # 각 노드별 시간 창 설정
    for location_idx, (start, end) in enumerate(time_windows):
        index = manager.NodeToIndex(location_idx)
        time_dimension.CumulVar(index).SetRange(start, end)
        
        # 0번(센터)을 제외하고, 우선순위에 따른 페널티 부여
        if location_idx > 0:
             order = target_orders[location_idx-1]
             # priority가 1(긴급)이면 미방문 시 페널티를 아주 크게 줌 (방문 강제)
             penalty = 1000000 if order.priority == 1 else 1000
             # 해당 노드를 방문하지 않아도 되는 옵션(Disjunction)을 주되, 페널티를 부과
             routing.AddDisjunction([index], penalty)

    # (6) 해 찾기 (Solve)
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    # 초기 해 탐색 전략: 가장 저렴한 경로 우선
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    # 계산 시간 제한: 10초
    search_parameters.time_limit.seconds = 10

    solution = routing.SolveWithParameters(search_parameters)

    # (7) 결과 포맷팅
    routes_result = []
    if solution:
        for vehicle_id in range(num_vehicles):
            index = routing.Start(vehicle_id)
            route_path = []
            route_load = 0
            
            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                route_load += demands[node_index]
                
                # 도착 시간 정보 (최소 가능 시간)
                time_var = time_dimension.CumulVar(index)
                arrival_time = solution.Min(time_var)
                
                route_path.append({
                    "location": location_names[node_index],
                    "arrival_time": arrival_time,
                    "load_collected": demands[node_index]
                })
                # 다음 방문지로 이동
                index = solution.Value(routing.NextVar(index))
            
            # 마지막 복귀 지점(센터) 추가
            node_index = manager.IndexToNode(index)
            time_var = time_dimension.CumulVar(index)
            route_path.append({
                "location": location_names[node_index],
                "arrival_time": solution.Min(time_var),
                "load_collected": 0
            })

            # 실제로 이동한 경로만 결과에 포함 (출발-도착만 있으면 제외)
            if len(route_path) > 2: 
                routes_result.append({
                    "vehicle_id": target_vehicles[vehicle_id].차량번호,
                    "total_load": route_load,
                    "capacity": target_vehicles[vehicle_id].수송용량,
                    "path": route_path
                })
    
    return {"status": "success", "routes": routes_result}


# ==========================================
# 4. API 엔드포인트 (n8n이 호출하는 곳)
# ==========================================

@app.post("/optimize")
def optimize_endpoint(req: OptimizationRequest):
    print(f"📥 배차 요청 수신: 주문 {len(req.orders)}건, 차량 {len(req.vehicles)}대")
    
    # 1. 휘발유 배차 실행
    gas_result = solve_vrp_algorithm(req.orders, req.vehicles, "휘발유")
    
    # 2. 등경유(등유+경유) 배차 실행
    diesel_result = solve_vrp_algorithm(req.orders, req.vehicles, "등경유")
    
    return {
        "gasoline_dispatch": gas_result,
        "diesel_dispatch": diesel_result
    }

@app.get("/")
def health_check():
    # 서버가 살았는지, 데이터는 잘 로드됐는지 확인하는 용도
    matrix_status = "Loaded" if len(MATRIX_DATA) > 0 else "Empty"
    return {
        "status": "ok", 
        "message": "Jeju VRP Solver is running", 
        "matrix_status": matrix_status
    }
