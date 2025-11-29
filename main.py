import json
import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# OR-Tools 라이브러리 (Railway 환경에 설치되어 있어야 함)
try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    print("❌ OR-Tools가 설치되지 않았습니다.")

app = FastAPI()

# --- 1. 데이터 모델 정의 (n8n에서 보낼 데이터와 일치해야 함) ---
class OrderItem(BaseModel):
    주유소명: str
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    start_min: int = 540  # 기본값 09:00
    end_min: int = 1080   # 기본값 18:00
    priority: int = 2     # 1:긴급, 2:보통

class VehicleItem(BaseModel):
    차량번호: str
    유종: str  # "휘발유" 또는 "등경유"
    수송용량: int

class OptimizationRequest(BaseModel):
    orders: List[OrderItem]
    vehicles: List[VehicleItem]

# --- 2. 거리 매트릭스 로드 (서버 시작시 1번만) ---
# 주의: 이 파일(jeju_distance_matrix_full.json)도 Railway에 같이 올려야 합니다.
MATRIX_DATA = {}
try:
    if os.path.exists("jeju_distance_matrix_full.json"):
        with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
            raw_matrix = json.load(f)
            # 구조가 { "matrix": { ... } } 인 경우와 바로 { ... } 인 경우 대응
            if "matrix" in raw_matrix and isinstance(raw_matrix["matrix"], dict):
                MATRIX_DATA = raw_matrix["matrix"]
            else:
                MATRIX_DATA = raw_matrix
        print(f"✅ 거리 매트릭스 로드 완료 (노드 수: {len(MATRIX_DATA)})")
    else:
        print("⚠️ 경고: jeju_distance_matrix_full.json 파일이 없습니다.")
except Exception as e:
    print(f"❌ 매트릭스 로드 중 에러: {e}")

# --- 3. VRP 알고리즘 로직 ---
def solve_vrp_algorithm(orders, vehicles, fuel_type_filter):
    # (1) 해당 유종에 맞는 주문과 차량만 필터링
    target_orders = []
    for order in orders:
        # 휘발유 차량이면 휘발유 주문량, 아니면 등유+경유 주문량 합산
        amount = order.휘발유 if fuel_type_filter == "휘발유" else (order.등유 + order.경유)
        if amount > 0:
            target_orders.append(order)
            
    target_vehicles = [v for v in vehicles if v.유종 == fuel_type_filter]
    
    if not target_orders or not target_vehicles:
        return {"status": "skipped", "reason": "No orders or vehicles for this fuel type", "routes": []}

    # (2) 노드 리스트 생성 (0번은 항상 물류센터)
    depot = "제주물류센터"
    location_names = [depot] + [o.주유소명 for o in target_orders]
    num_locations = len(location_names)
    
    # (3) 거리/시간 매트릭스 구성
    time_matrix = [[0] * num_locations for _ in range(num_locations)]
    try:
        for i in range(num_locations):
            for j in range(num_locations):
                if i == j: continue
                origin = location_names[i]
                dest = location_names[j]
                
                # 매트릭스에서 조회 (없으면 기본값 30분)
                if origin in MATRIX_DATA and dest in MATRIX_DATA[origin]:
                    # 값은 분 단위 정수여야 함
                    time_matrix[i][j] = int(float(MATRIX_DATA[origin][dest]))
                else:
                    time_matrix[i][j] = 30 # 기본값
    except Exception as e:
        print(f"매트릭스 구성 중 에러: {e}")
        return {"status": "error", "message": str(e)}
    
    # (4) OR-Tools 데이터 모델 생성
    demands = [0] # 0번 노드(센터) 수요는 0
    for o in target_orders:
        amt = o.휘발유 if fuel_type_filter == "휘발유" else (o.등유 + o.경유)
        demands.append(amt)
        
    vehicle_capacities = [v.수송용량 for v in target_vehicles]
    num_vehicles = len(target_vehicles)
    
    time_windows = [(0, 1440)] # 0번 노드(센터)는 24시간 오픈
    for o in target_orders:
        time_windows.append((o.start_min, o.end_min))

    # (5) Solver 설정 및 실행
    manager = pywrapcp.RoutingIndexManager(num_locations, num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    # 비용 함수: 시간 최소화
    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return time_matrix[from_node][to_node]
    
    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # 제약 조건 1: 용량 (Capacity)
    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return demands[from_node]
    
    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index, 0, vehicle_capacities, True, 'Capacity'
    )

    # 제약 조건 2: 시간 (Time Windows)
    routing.AddDimension(
        transit_callback_index, 
        120,  # 대기 허용 시간 (일찍 도착 시 최대 대기)
        1440, # 차량 최대 운행 시간
        False, 
        'Time'
    )
    time_dimension = routing.GetDimensionOrDie('Time')
    
    # 각 지점별 시간 창 설정 및 우선순위 페널티
    for location_idx, (start, end) in enumerate(time_windows):
        index = manager.NodeToIndex(location_idx)
        time_dimension.CumulVar(index).SetRange(start, end)
        
        # 0번(센터) 제외하고 우선순위 적용
        if location_idx > 0:
             order = target_orders[location_idx-1]
             # 우선순위 1(긴급)이면 미방문 페널티를 매우 크게 주어 반드시 방문하게 함
             penalty = 1000000 if order.priority == 1 else 1000
             routing.AddDisjunction([index], penalty)

    # 해 찾기
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    search_parameters.time_limit.seconds = 10 # 최대 10초 계산

    solution = routing.SolveWithParameters(search_parameters)

    # (6) 결과 정리
    routes_result = []
    if solution:
        for vehicle_id in range(num_vehicles):
            index = routing.Start(vehicle_id)
            route_path = []
            route_load = 0
            
            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                route_load += demands[node_index]
                
                # 시간 정보 추출
                time_var = time_dimension.CumulVar(index)
                arrival_time = solution.Min(time_var)
                
                route_path.append({
                    "location": location_names[node_index],
                    "arrival_time": arrival_time,
                    "load": demands[node_index]
                })
                index = solution.Value(routing.NextVar(index))
            
            # 마지막 센터 복귀
            node_index = manager.IndexToNode(index)
            time_var = time_dimension.CumulVar(index)
            route_path.append({
                "location": location_names[node_index],
                "arrival_time": solution.Min(time_var),
                "load": 0
            })

            # 실제로 운행한 차량만 결과에 포함
            if len(route_path) > 2: 
                routes_result.append({
                    "vehicle_id": target_vehicles[vehicle_id].차량번호,
                    "total_load": route_load,
                    "capacity": target_vehicles[vehicle_id].수송용량,
                    "path": route_path
                })
    
    return {"status": "success", "routes": routes_result}

# --- 4. API 엔드포인트 ---
@app.post("/optimize")
def optimize_endpoint(req: OptimizationRequest):
    # 1. 휘발유 배차 실행
    gas_result = solve_vrp_algorithm(req.orders, req.vehicles, "휘발유")
    
    # 2. 등경유(등유+경유) 배차 실행
    # 차량 유종 필드가 '등경유'로 되어 있어야 매칭됩니다.
    diesel_result = solve_vrp_algorithm(req.orders, req.vehicles, "등경유")
    
    return {
        "gasoline_dispatch": gas_result,
        "diesel_dispatch": diesel_result
    }

@app.get("/")
def health_check():
    return {"status": "ok", "matrix_ready": len(MATRIX_DATA) > 0}
