import json
import os
import requests
import math
from typing import List, Dict, Any
from fastapi import FastAPI
from pydantic import BaseModel, model_validator

try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    print("❌ OR-Tools 설치 필요")

app = FastAPI()

# ==========================================
# 1. 시간 설정 (분 단위)
# ==========================================
# 07:00 = 420분
DRIVER_START_TIME = 420 
# 점심시간: 12:00(720분) ~ 13:00(780분), 60분간
LUNCH_START = 720
LUNCH_DURATION = 60
# 업무 마감: 18:00 (1080분) -> 복귀 마지노선
WORK_END_TIME = 1080

LOADING_TIME = 30      # 상차 시간
UNLOADING_TIME = 30    # 하역 시간

# 환경변수 읽기
NAVER_ID = os.environ.get("NAVER_CLIENT_ID") or os.environ.get("x-ncp-apigw-api-key-id")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET") or os.environ.get("x-ncp-apigw-api-key")

if not NAVER_ID or not NAVER_SECRET:
    print("⚠️ [경고] 네이버 지도 API 키가 설정되지 않았습니다.")
else:
    masked = NAVER_ID[:2] + "*" * 4
    print(f"✅ 네이버 지도 API 키 로드됨 ({masked})")

# ==========================================
# 2. 데이터 모델
# ==========================================
class OrderItem(BaseModel):
    주유소명: str
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    # 기본 배송 가능 시간은 업무 시간 전체로 설정 (점심시간은 로직에서 제외시킴)
    start_min: int = 420  
    end_min: int = 1080
    priority: int = 2
    
    class Config:
        extra = 'allow'

    @model_validator(mode='before')
    @classmethod
    def flatten_data(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ['주문량', 'order', 'data']:
                if key in data and isinstance(data[key], dict):
                    inner = data[key]
                    if any(k in inner for k in ['휘발유', '경유', '등유']):
                        data.update(inner)
            for field in ['휘발유', '등유', '경유', 'start_min', 'end_min', 'priority']:
                if field in data:
                    try:
                        if data[field] == "" or data[field] is None:
                            data[field] = 0
                        else:
                            data[field] = int(data[field])
                    except:
                        data[field] = 0
        return data

class VehicleItem(BaseModel):
    차량번호: str
    유종: str
    수송용량: int

class OptimizationRequest(BaseModel):
    orders: List[OrderItem]
    vehicles: List[VehicleItem]

# ==========================================
# 3. 데이터 로드 & 네이버 API
# ==========================================
NODE_INFO = {}
MATRIX_DATA = {}
DIST_CACHE = {}
PATH_CACHE = {}

def load_data():
    global NODE_INFO, MATRIX_DATA
    raw_data = None
    url = os.environ.get("JEJU_MATRIX_URL")
    if url:
        try:
            res = requests.get(url, timeout=15)
            if res.status_code == 200: raw_data = res.json()
        except: pass
    
    if not raw_data and os.path.exists("jeju_distance_matrix_full.json"):
        try:
            with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except: pass

    if raw_data:
        if isinstance(raw_data, list) and len(raw_data) > 0:
            raw_data = raw_data[0]
        if "node_info" in raw_data:
            for node in raw_data["node_info"]:
                NODE_INFO[node["name"]] = {"lat": node["lat"], "lon": node["lon"]}
        if "matrix" in raw_data:
            MATRIX_DATA = raw_data["matrix"]
        else:
            MATRIX_DATA = raw_data
        print(f"✅ 데이터 준비 완료 (노드 {len(NODE_INFO)}개, 매트릭스 {len(MATRIX_DATA)}개)")

load_data()

def get_driving_time(start_name, end_name):
    key = f"{start_name}->{end_name}"
    if key in DIST_CACHE: return DIST_CACHE[key]
    
    # 1순위: 파일 매트릭스 (속도)
    if start_name in MATRIX_DATA and end_name in MATRIX_DATA[start_name]:
        try:
            dist = float(MATRIX_DATA[start_name][end_name])
            # 거리(km)기반 시간 추정: 시속 35km/h (제주 시내/국도 복합)
            minutes = int((dist / 35) * 60)
            return max(5, minutes)
        except: pass

    # 2순위: 네이버 API
    if start_name in NODE_INFO and end_name in NODE_INFO and NAVER_ID:
        try:
            url = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
            headers = {
                "x-ncp-apigw-api-key-id": NAVER_ID,
                "x-ncp-apigw-api-key": NAVER_SECRET
            }
            s = NODE_INFO[start_name]
            g = NODE_INFO[end_name]
            params = {
                "start": f"{s['lon']},{s['lat']}",
                "goal": f"{g['lon']},{g['lat']}",
                "option": "trafast"
            }
            res = requests.get(url, headers=headers, params=params, timeout=2)
            if res.status_code == 200 and res.json()["code"] == 0:
                m = int(res.json()["route"]["trafast"][0]["summary"]["duration"] / 60000)
                DIST_CACHE[key] = m
                return m
        except: pass

    return 20 # 기본값

def get_detailed_path_geometry(start_name, end_name):
    key = f"{start_name}->{end_name}"
    if key in PATH_CACHE: return PATH_CACHE[key]
    if start_name not in NODE_INFO or end_name not in NODE_INFO or not NAVER_ID: return []

    try:
        url = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
        headers = {
            "x-ncp-apigw-api-key-id": NAVER_ID,
            "x-ncp-apigw-api-key": NAVER_SECRET
        }
        s = NODE_INFO[start_name]
        g = NODE_INFO[end_name]
        params = {
            "start": f"{s['lon']},{s['lat']}",
            "goal": f"{g['lon']},{g['lat']}",
            "option": "trafast"
        }
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json()["code"] == 0:
            path = res.json()["route"]["trafast"][0]["path"]
            PATH_CACHE[key] = path
            return path
    except: pass
    return []

# ==========================================
# 4. 배차 알고리즘 (점심시간 적용)
# ==========================================

def solve_multitrip_vrp(all_orders, all_vehicles, fuel_type):
    debug_logs = []
    pending_orders = []
    for o in all_orders:
        amt = o.휘발유 if fuel_type == "휘발유" else (o.등유 + o.경유)
        if amt > 0: pending_orders.append(o)

    my_vehicles = [v for v in all_vehicles if v.유종 == fuel_type]
    
    if not pending_orders or not my_vehicles:
        return {"status": "skipped", "routes": [], "debug_logs": []}

    # 모든 차량 07:00 시작
    vehicle_state = {i: DRIVER_START_TIME for i in range(len(my_vehicles))} 
    final_schedule = []
    
    # 회차 반복 (최대 5회)
    for round_num in range(1, 6):
        if not pending_orders: break
        
        # 업무 종료(18:00) 1시간 전까지만 출발 가능
        available_indices = [i for i, t in vehicle_state.items() if t < WORK_END_TIME - 60]
        if not available_indices: break
        
        current_vehicles = [my_vehicles[i] for i in available_indices]
        current_starts = [vehicle_state[i] for i in available_indices]
        
        # 점심시간 보정: 만약 출발 시간이 11:30 ~ 13:00 사이라면, 13:00 이후로 강제 이동
        # (점심 먹고 출발해라)
        adjusted_starts = []
        for t in current_starts:
            # 점심시간에 걸치거나 너무 가까우면 13:00(780분)으로 미룸
            if t > LUNCH_START - 30 and t < LUNCH_START + LUNCH_DURATION:
                adjusted_starts.append(LUNCH_START + LUNCH_DURATION)
            else:
                adjusted_starts.append(t)
        
        routes, remaining = run_ortools(pending_orders, current_vehicles, adjusted_starts, fuel_type)
        
        if not routes and len(remaining) == len(pending_orders):
            break

        for r in routes:
            real_v_idx = available_indices[r['internal_idx']]
            
            # 복귀 후 다음 출발 시간 계산
            next_time = r['end_time'] + LOADING_TIME
            
            # 만약 운행 중에 점심시간이 끼었다면? (도착시간이 12시를 넘김)
            # OR-Tools에서 IntervalVar로 처리했으므로 r['end_time']에 이미 반영되어 있을 것임.
            # 혹시 모르니 다음 출발 시간이 점심시간이면 뒤로 미룸
            if next_time >= LUNCH_START and next_time < LUNCH_START + LUNCH_DURATION:
                next_time = LUNCH_START + LUNCH_DURATION

            vehicle_state[real_v_idx] = next_time
            r['round'] = round_num
            r['vehicle_id'] = my_vehicles[real_v_idx].차량번호
            final_schedule.append(r)
            
        pending_orders = remaining

    skipped_list = [{"name": o.주유소명} for o in pending_orders]

    return {
        "status": "success", 
        "total_delivered": sum(r['total_load'] for r in final_schedule),
        "routes": final_schedule, 
        "unassigned_orders": skipped_list,
        "debug_logs": debug_logs
    }

def run_ortools(orders, vehicles, start_times, fuel_type):
    depot = "제주물류센터"
    locs = [depot] + [o.주유소명 for o in orders]
    N = len(locs)
    
    durations = [[0]*N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i != j: durations[i][j] = get_driving_time(locs[i], locs[j])

    manager = pywrapcp.RoutingIndexManager(N, len(vehicles), 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_i, to_i):
        f, t = manager.IndexToNode(from_i), manager.IndexToNode(to_i)
        service = UNLOADING_TIME if t != 0 else 0
        return durations[f][t] + service

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    
    # 시간 차원 추가 (최대 24시간)
    routing.AddDimension(transit_idx, 1440, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    
    # 1. 차량별 출발 시간 설정
    for i in range(len(vehicles)):
        idx = routing.Start(i)
        time_dim.CumulVar(idx).SetMin(int(start_times[i]))

    # 2. 점심시간(휴식) 설정: 12:00~13:00 (720~780)
    # 차량이 이 시간에는 어떤 노드도 방문하거나 이동을 완료할 수 없게 함
    solver = routing.solver()
    for i in range(len(vehicles)):
        # 12시에 시작해서 60분간 지속되는 '휴식' 간격 추가
        # ForceStart=True(720분에 강제 시작)
        lunch_break = solver.FixedDurationIntervalVar(LUNCH_START, LUNCH_START, LUNCH_DURATION, False, "Lunch")
        time_dim.SetBreakIntervalsOfVehicle([lunch_break], i, [])

    # 3. 주유소별 시간 창
    time_dim.CumulVar(routing.Start(0)).SetRange(0, 1440)
    for i, order in enumerate(orders):
        index = manager.NodeToIndex(i + 1)
        time_dim.CumulVar(index).SetRange(order.start_min, order.end_min)
        penalty = 100000 if order.priority == 1 else 1000
        routing.AddDisjunction([index], penalty)

    # 4. 용량 제약
    demands = [0] + [ (o.휘발유 if fuel_type=="휘발유" else o.등유+o.경유) for o in orders ]
    def demand_callback(from_i):
        return demands[manager.IndexToNode(from_i)]
    cap_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(cap_idx, 0, [v.수송용량 for v in vehicles], True, "Capacity")

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.time_limit.seconds = 5
    
    solution = routing.SolveWithParameters(search_params)
    
    routes = []
    fulfilled_indices = set()
    
    if solution:
        for v_idx in range(len(vehicles)):
            index = routing.Start(v_idx)
            path = []
            load = 0
            geometry_list = []

            while not routing.IsEnd(index):
                node_idx = manager.IndexToNode(index)
                if node_idx > 0: fulfilled_indices.add(node_idx - 1)
                
                t_val = solution.Min(time_dim.CumulVar(index))
                node_name = locs[node_idx]
                coord = NODE_INFO.get(node_name, {"lat": 0, "lon": 0})
                
                path.append({
                    "location": node_name,
                    "lat": coord["lat"], "lon": coord["lon"],
                    "time": t_val, "load": demands[node_idx]
                })
                load += demands[node_idx]

                next_index = solution.Value(routing.NextVar(index))
                if not routing.IsEnd(next_index):
                    next_node_idx = manager.IndexToNode(next_index)
                    segment_path = get_detailed_path_geometry(node_name, locs[next_node_idx])
                    if segment_path: geometry_list.extend(segment_path)
                
                index = next_index

            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            depot_coord = NODE_INFO.get(depot, {"lat": 0, "lon": 0})
            
            # 마지막 복귀 경로
            last_loc = path[-1]["location"]
            return_path = get_detailed_path_geometry(last_loc, depot)
            if return_path: geometry_list.extend(return_path)
            
            path.append({
                "location": depot,
                "lat": depot_coord["lat"], "lon": depot_coord["lon"],
                "time": end_time, "load": 0
            })
            
            if len(path) > 2:
                routes.append({
                    "internal_idx": v_idx, 
                    "end_time": end_time, 
                    "total_load": load, 
                    "path": path,
                    "geometry": geometry_list
                })
                
    remaining = [orders[i] for i in range(len(orders)) if i not in fulfilled_indices]
    return routes, remaining

@app.post("/optimize")
def optimize(req: OptimizationRequest):
    gas = solve_multitrip_vrp(req.orders, req.vehicles, "휘발유")
    diesel = solve_multitrip_vrp(req.orders, req.vehicles, "등경유")
    return {"gasoline": gas, "diesel": diesel}

@app.get("/")
def health():
    return {"status": "ok"}
