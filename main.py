import json
import os
import requests
import math
from typing import List, Dict, Any
from fastapi import FastAPI
from pydantic import BaseModel

# OR-Tools
try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    print("❌ OR-Tools 설치 필요")

app = FastAPI()

# ==========================================
# 1. 설정 및 데이터 모델
# ==========================================

# 작업 소요 시간 설정 (분 단위)
LOADING_TIME = 20   # 센터에서 기름 넣는 시간
UNLOADING_TIME = 30 # 주유소에서 기름 내리는 시간
RETURN_SPEED_KPH = 60 # 복귀 시 평균 시속 (직선거리용 백업)

# 네이버 API 키 (Railway Variables에서 설정)
NAVER_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET")

class OrderItem(BaseModel):
    주유소명: str
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    start_min: int = 540  # 09:00
    end_min: int = 1080   # 18:00
    priority: int = 2     # 1:긴급

class VehicleItem(BaseModel):
    차량번호: str
    유종: str
    수송용량: int

class OptimizationRequest(BaseModel):
    orders: List[OrderItem]
    vehicles: List[VehicleItem]

# ==========================================
# 2. 데이터 로드 & 네이버 거리 계산
# ==========================================
NODE_INFO = {} # 좌표 정보 { "주유소명": {"lat": 33..., "lon": 126...} }
DIST_CACHE = {} # 거리 계산 캐시

def load_basic_data():
    global NODE_INFO
    # 환경변수 URL 우선
    url = os.environ.get("JEJU_MATRIX_URL")
    data = None
    
    if url:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200: data = res.json()
        except: pass
    
    if not data and os.path.exists("jeju_distance_matrix_full.json"):
        with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            
    if data:
        # node_info 리스트를 딕셔너리로 변환
        if "node_info" in data:
            for node in data["node_info"]:
                NODE_INFO[node["name"]] = {"lat": node["lat"], "lon": node["lon"]}
        print(f"✅ 좌표 데이터 로드 완료: {len(NODE_INFO)}개 지점")

load_basic_data()

def get_driving_time(start_name, end_name):
    # 1. 캐시 확인
    key = f"{start_name}->{end_name}"
    if key in DIST_CACHE: return DIST_CACHE[key]
    
    # 2. 좌표 확인
    if start_name not in NODE_INFO or end_name not in NODE_INFO:
        return 30 # 좌표 없으면 기본 30분
        
    start = NODE_INFO[start_name]
    goal = NODE_INFO[end_name]
    
    # 3. 네이버 API 호출
    if NAVER_ID and NAVER_SECRET:
        try:
            url = "https://naveropenapi.apigw.ntruss.com/map-direction/v1/driving"
            headers = {
                "X-NCP-APIGW-API-KEY-ID": NAVER_ID,
                "X-NCP-APIGW-API-KEY": NAVER_SECRET
            }
            params = {
                "start": f"{start['lon']},{start['lat']}",
                "goal": f"{goal['lon']},{goal['lat']}",
                "option": "trafast" # 실시간 빠른길
            }
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                json_res = res.json()
                if json_res["code"] == 0:
                    duration_ms = json_res["route"]["trafast"][0]["summary"]["duration"]
                    minutes = int(duration_ms / 60000)
                    DIST_CACHE[key] = minutes
                    return minutes
        except Exception as e:
            print(f"Naver API Error: {e}")

    # 4. API 실패 시 하버사인 공식으로 직선거리 시간 추정
    R = 6371
    dLat = math.radians(goal['lat'] - start['lat'])
    dLon = math.radians(goal['lon'] - start['lon'])
    a = math.sin(dLat/2)**2 + math.cos(math.radians(start['lat'])) * math.cos(math.radians(goal['lat'])) * math.sin(dLon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    dist_km = R * c
    # 시속 40km 가정 + 신호대기 1.3배
    est_time = int((dist_km / 40) * 60 * 1.3)
    return max(5, est_time) # 최소 5분

# ==========================================
# 3. 다회전 배차 알고리즘 (핵심)
# ==========================================

def solve_multitrip_vrp(all_orders, all_vehicles, fuel_type):
    # 해당 유종 데이터 필터링
    pending_orders = []
    for o in all_orders:
        amt = o.휘발유 if fuel_type == "휘발유" else (o.등유 + o.경유)
        if amt > 0: pending_orders.append(o)
        
    my_vehicles = [v for v in all_vehicles if v.유종 == fuel_type]
    
    if not pending_orders or not my_vehicles:
        return {"status": "skipped", "routes": []}

    # 차량 상태 초기화 (모든 차량 09:00 시작)
    # vehicle_state[i] = 배차 가능한 시간 (분)
    vehicle_state = {i: 540 for i in range(len(my_vehicles))} 
    
    final_schedule = []
    
    # 반복 배차 (최대 5회전까지 시도)
    for round_num in range(1, 6):
        if not pending_orders: break # 주문 다 처리했으면 끝
        
        # 현재 배차 가능한 차량 선별 (18:00 이전 복귀 가능한 차만)
        available_indices = [i for i, t in vehicle_state.items() if t < 1020] # 17:00 이후엔 출발 안함
        if not available_indices: break
        
        # 이번 회차용 차량 리스트 구성
        current_round_vehicles = [my_vehicles[i] for i in available_indices]
        current_round_start_times = [vehicle_state[i] for i in available_indices]
        
        # OR-Tools 실행 (1회전)
        round_routes, remaining = run_single_vrp(
            pending_orders, 
            current_round_vehicles, 
            current_round_start_times,
            fuel_type
        )
        
        # 결과 저장 및 상태 업데이트
        for r in round_routes:
            # r['vehicle_index']는 available_indices 리스트 내의 인덱스임
            real_v_idx = available_indices[r['internal_idx']]
            
            # 복귀 시간 + 상차 시간 = 다음 출발 시간
            next_start_time = r['end_time'] + LOADING_TIME
            vehicle_state[real_v_idx] = next_start_time
            
            # 결과에 회차 정보 추가
            r['round'] = round_num
            r['vehicle_id'] = my_vehicles[real_v_idx].차량번호
            final_schedule.append(r)
            
        # 남은 주문으로 업데이트
        pending_orders = remaining

    return {"status": "success", "routes": final_schedule, "remaining_orders": len(pending_orders)}

def run_single_vrp(orders, vehicles, start_times, fuel_type):
    # 1. 노드 구성
    depot = "제주물류센터"
    locs = [depot] + [o.주유소명 for o in orders]
    N = len(locs)
    
    # 2. 매트릭스 생성 (필요한 부분만 API 호출)
    durations = [[0]*N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i != j:
                durations[i][j] = get_driving_time(locs[i], locs[j])

    # 3. OR-Tools 설정
    manager = pywrapcp.RoutingIndexManager(N, len(vehicles), 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_i, to_i):
        f = manager.IndexToNode(from_i)
        t = manager.IndexToNode(to_i)
        travel = durations[f][t]
        # 도착지 하역 시간 추가 (출발지/복귀지 제외)
        service = UNLOADING_TIME if t != 0 else 0
        return travel + service

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    
    # 시간 제약 (차량별 출발 시간이 다름!)
    routing.AddDimension(transit_idx, 1000, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    
    # 차량별 시작 시간 설정
    for i in range(len(vehicles)):
        idx = routing.Start(i)
        time_dim.CumulVar(idx).SetMin(int(start_times[i]))

    # 용량 제약
    demands = [0] + [ (o.휘발유 if fuel_type=="휘발유" else o.등유+o.경유) for o in orders ]
    def demand_callback(from_i):
        return demands[manager.IndexToNode(from_i)]
    
    cap_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        cap_idx, 0, [v.수송용량 for v in vehicles], True, "Capacity"
    )

    # 4. 풀기
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.time_limit.seconds = 5
    
    solution = routing.SolveWithParameters(search_params)
    
    # 5. 결과 파싱
    routes = []
    fulfilled_indices = set()
    
    if solution:
        for v_idx in range(len(vehicles)):
            index = routing.Start(v_idx)
            path = []
            load = 0
            
            while not routing.IsEnd(index):
                node_idx = manager.IndexToNode(index)
                if node_idx > 0: fulfilled_indices.add(node_idx - 1)
                
                t_val = solution.Min(time_dim.CumulVar(index))
                path.append({
                    "location": locs[node_idx],
                    "time": t_val,
                    "load": demands[node_idx]
                })
                load += demands[node_idx]
                index = solution.Value(routing.NextVar(index))
            
            # 복귀
            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            path.append({"location": depot, "time": end_time, "load": 0})
            
            if len(path) > 2:
                routes.append({
                    "internal_idx": v_idx,
                    "end_time": end_time,
                    "total_load": load,
                    "path": path
                })
                
    remaining = [orders[i] for i in range(len(orders)) if i not in fulfilled_indices]
    return routes, remaining

# ==========================================
# 4. 엔드포인트
# ==========================================
@app.post("/optimize")
def optimize(req: OptimizationRequest):
    gas = solve_multitrip_vrp(req.orders, req.vehicles, "휘발유")
    diesel = solve_multitrip_vrp(req.orders, req.vehicles, "등경유")
    return {"gasoline": gas, "diesel": diesel}

@app.get("/")
def health():
    return {"status": "ok", "naver_api": bool(NAVER_ID)}
