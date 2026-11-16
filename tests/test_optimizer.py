import itertools
import math
import random
from queuebot.optimizer import Assignment, Candidate, satisfaction


def test_one_participant():
    obj=Assignment([Candidate(1,'exact',1)])
    assert obj.draw(1)=={1:1}
    assert obj.score(obj.draw(1))>=obj.floor


def test_two_with_priorities():
    people=[Candidate(1,'exact',5,priority=True,priority_order=5),
            Candidate(2,'exact',1,priority=True,priority_order=2),
            Candidate(3,'exact',1),Candidate(4,'exact',4)]
    obj=Assignment(people)
    result=obj.draw(123)
    assert result[2]==1 and result[1]==2
    assert sorted(result.values())==[1,2,3,4]
    assert obj.score(result)>=obj.floor


def test_optimality_and_95_percent():
    for n in range(2,6):
        people=[Candidate(i,'exact',i//2+1,balance=(-100 if i%2 else 100)) for i in range(n)]
        obj=Assignment(people)
        best=max(sum(satisfaction(p,order[i]+1,n) for i,p in enumerate(people))
                 for order in itertools.permutations(range(n)))
        assert obj.max_utility==best
        assert obj.floor==math.ceil(.95*best)
        for seed in range(10):
            found=obj.draw(seed)
            assert sorted(found.values())==list(range(1,n+1))
            assert obj.score(found)>=obj.floor


def test_seed_reproducible():
    a=Assignment([Candidate(i,'exact',3) for i in range(1,7)])
    assert a.draw(1234)==a.draw(1234)


def test_any_pref_no_penalty():
    assert satisfaction(Candidate(1,'any'),1,10)==100
    assert satisfaction(Candidate(1,'any'),10,10)==100


def test_exact_above_participants_clamps_to_last():
    assert satisfaction(Candidate(1,'exact',19),3,3)==100


def test_unsupported_preference_modes_removed():
    import pytest
    with pytest.raises(ValueError):
        Assignment([Candidate(1,'middle')])


def test_random_mode_is_actually_random():
    people = [Candidate(i, 'any') for i in range(1, 7)]
    assignment = Assignment(people)
    outputs = {tuple(assignment.draw(seed)[i] for i in range(1, 7)) for seed in range(15)}
    assert len(outputs) >= 3
    assert all(sorted(order)==list(range(1,7)) for order in outputs)
