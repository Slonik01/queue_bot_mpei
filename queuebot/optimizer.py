"""Two-step assignment: maximize utility, then maximize weighted utility with >=95% floor.

Priority users receive positions 1..k in their previous-lesson order.
All other users are optimized for remaining positions.
"""
from dataclasses import dataclass
import math
import random
try:
    from ortools.sat.python import cp_model
except ImportError:
    cp_model = None



@dataclass(frozen=True)
class Candidate:
    user_id: int
    mode: str = 'any'
    desired: int | None = None
    balance: float = 0.0
    priority: bool = False
    priority_order: int = 0
    fairness_existed: bool = False


def satisfaction(person: Candidate, position: int, total: int) -> int:
    if person.mode == 'any':
        return 100
    if person.mode == 'exact':
        target = max(1, min(total, person.desired or 1))
    else:
        raise ValueError('Unknown preference mode')
    return max(0, 100 - 15 * abs(position - target))


def _model(people, positions, total, floor=None, weights=None, noise=None):
    if cp_model is None:
        raise RuntimeError('OR-Tools is not installed')
    model = cp_model.CpModel()
    n = len(people)
    xs = [[model.new_bool_var(f'assign_{i}_{j}') for j in range(n)] for i in range(n)]
    for i in range(n):
        model.add_exactly_one(xs[i])
    for j in range(n):
        model.add_exactly_one(xs[i][j] for i in range(n))
    util = [[satisfaction(p, pos, total) for pos in positions] for p in people]
    raw = sum(util[i][j] * xs[i][j] for i in range(n) for j in range(n))
    if floor is not None:
        model.add(raw >= floor)
    if weights is None:
        model.maximize(raw)
    else:
        model.maximize(sum((weights[i] * util[i][j] + noise[i][j]) * xs[i][j]
                           for i in range(n) for j in range(n)))
    return model, xs, util


def _solve(model, xs, max_seconds=8):
    if cp_model is None:
        raise RuntimeError('OR-Tools is not installed')
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_seconds
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status != cp_model.OPTIMAL:
        # Never silently violate the 95% guarantee or pretend a suboptimal first stage is optimal.
        raise RuntimeError(f'Точный оптимум не найден (статус {solver.status_name(status)}).')
    return solver, {i: next(j for j in range(len(xs)) if solver.value(xs[i][j]))
                    for i in range(len(xs))}


class Assignment:
    def __init__(self, candidates: list[Candidate]):
        self.people = list(candidates)
        self.total = len(candidates)
        if len({p.user_id for p in candidates}) != self.total:
            raise ValueError('Duplicate user IDs')
        self.priority = sorted((p for p in candidates if p.priority),
                               key=lambda p: (p.priority_order, p.user_id))
        self.ordinary = [p for p in candidates if not p.priority]
        self.fixed = {p.user_id: i+1 for i, p in enumerate(self.priority)}
        self.positions = list(range(len(self.priority)+1, self.total+1))
        if self.ordinary:
            if cp_model is None:
                self.max_utility = self._scipy_solve()[1]
            else:
                mod, xs, _ = _model(self.ordinary, self.positions, self.total)
                solver, _ = _solve(mod, xs)
                self.max_utility = int(round(solver.objective_value))
            self.floor = (95 * self.max_utility + 99) // 100
        else:
            self.max_utility = self.floor = 0

    def draw(self, seed: int, use_history=True, max_seconds=8) -> dict[int, int]:
        result = dict(self.fixed)
        if not self.ordinary:
            return result
        rng = random.Random(seed)
        weights = []
        for p in self.ordinary:
            balance = p.balance if use_history else 0
            weight = 1000 + round(2 * max(-100, min(100, balance)))
            weight += rng.randint(-20, 20)
            weights.append(weight)
        # Cell-specific tie-break: when everybody chose random, the result must be
        # a genuinely random permutation, not the same deterministic solver output.
        noise = [[rng.randint(-150, 150) for _ in self.positions] for _ in self.ordinary]
        if cp_model is None:
            assignment, _ = self._scipy_solve(self.floor, weights, noise)
        else:
            mod, xs, _ = _model(self.ordinary, self.positions, self.total,
                                self.floor, weights, noise)
            _, assignment = _solve(mod, xs, max_seconds)
        for i, col in assignment.items():
            result[self.ordinary[i].user_id] = self.positions[col]
        return result

    def _scipy_solve(self, floor=None, weights=None, noise=None):
        """Exact HiGHS MILP fallback; useful when OR-Tools is unavailable."""
        import numpy as np
        from scipy.optimize import linear_sum_assignment, milp, Bounds, LinearConstraint
        from scipy.sparse import lil_matrix
        n=len(self.ordinary)
        util=np.array([[satisfaction(p,pos,self.total) for pos in self.positions]
                       for p in self.ordinary],dtype=np.int64)
        if floor is None:
            row,col=linear_sum_assignment(-util)
            return dict(zip(row.tolist(),col.tolist())),int(util[row,col].sum())
        A=lil_matrix((2*n+1,n*n),dtype=np.float64)
        for i in range(n):
            for j in range(n):
                A[i,i*n+j]=1
                A[n+j,i*n+j]=1
        A[2*n,:]=util.reshape(-1)
        lb=np.array([1.]*(2*n)+[float(floor)])
        ub=np.array([1.]*(2*n)+[float('inf')])
        objective=-(util*np.array(weights)[:,None]+np.array(noise)).reshape(-1).astype(float)
        result=milp(c=objective,integrality=np.ones(n*n),
                    bounds=Bounds(np.zeros(n*n),np.ones(n*n)),
                    constraints=LinearConstraint(A.tocsr(),lb,ub),
                    options={'time_limit':8})
        if result.status!=0:
            raise RuntimeError(f'HiGHS exact optimum not found (status {result.status})')
        grid=result.x.reshape(n,n)
        assignment={i:int(grid[i].argmax()) for i in range(n)}
        if len(set(assignment.values()))!=n or sum(util[i,j] for i,j in assignment.items())<floor:
            raise RuntimeError('Invalid MILP assignment')
        return assignment,int(sum(util[i,j] for i,j in assignment.items()))

    def score(self, assignments: dict[int, int]) -> int:
        return sum(satisfaction(p, assignments[p.user_id], self.total)
                   for p in self.ordinary)

    def simulate(self, seed: int, count: int, use_history=True) -> dict[int, dict[int, int]]:
        rng = random.Random(seed)
        result = {p.user_id: {} for p in self.people}
        for _ in range(count):
            allocation = self.draw(rng.getrandbits(63), use_history=use_history,
                                   max_seconds=3)
            for uid, position in allocation.items():
                result[uid][position] = result[uid].get(position,0) + 1
        return result

    def baseline_expected(self, seed: int, samples: int = 50) -> dict[int, float]:
        counts = self.simulate(seed, samples, use_history=False)
        return {p.user_id: sum(n * satisfaction(p, position, self.total)
                               for position, n in counts[p.user_id].items()) / samples
                for p in self.people}
