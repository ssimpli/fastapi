from fastapi import FastAPI
from ortools.linear_solver import pywraplp

app = FastAPI()

@app.get("/")
def root():
    return {"greeting": "Hello, World!", "message": "Welcome to FastAPI!"}

@app.post("/optimize")
def optimize():
    solver = pywraplp.Solver.CreateSolver('GLOP')

    x = solver.NumVar(0, 5, 'x')
    y = solver.NumVar(0, 3, 'y')

    solver.Maximize(x + y)
    solver.Solve()

    return {
        "x": x.solution_value(),
        "y": y.solution_value(),
        "objective": solver.Objective().Value()
    }
