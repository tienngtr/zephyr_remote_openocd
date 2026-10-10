----------------------------- MODULE OutputOrder -----------------------------
EXTENDS Naturals, Sequences
VARIABLE q, produced, delivered, failed, final, bad
vars == <<q, produced, delivered, failed, final, bad>>
Streams == {"stdout", "stderr"}
Init == /\ q = [k \in Streams |-> <<>>]
    /\ produced = [k \in Streams |-> 0] /\ delivered = [k \in Streams |-> 0]
    /\ failed = FALSE /\ final = FALSE /\ bad = FALSE
Produce(k) == /\ ~failed /\ ~final /\ produced[k] < 3 /\ Len(q[k]) < 2
    /\ q' = [q EXCEPT ![k] = Append(@, produced[k] + 1)]
    /\ produced' = [produced EXCEPT ![k] = @ + 1]
    /\ UNCHANGED <<delivered, failed, final, bad>>
Relay(k) == /\ ~failed /\ Len(q[k]) > 0
    /\ q' = [q EXCEPT ![k] = Tail(@)]
    /\ delivered' = [delivered EXCEPT ![k] = Head(q[k])]
    /\ bad' = (bad \/ Head(q[k]) # delivered[k] + 1)
    /\ UNCHANGED <<produced, failed, final>>
Failure == /\ ~failed /\ failed' = TRUE
    /\ UNCHANGED <<q, produced, delivered, final, bad>>
Finalize == /\ ~final /\ (failed \/ \A k \in Streams : Len(q[k]) = 0)
    /\ final' = TRUE /\ UNCHANGED <<q, produced, delivered, failed, bad>>
Next == (\E k \in Streams : Produce(k) \/ Relay(k)) \/ Failure \/ Finalize
Spec == Init /\ [][Next]_vars
BoundedOrdered == ~bad /\ \A k \in Streams : Len(q[k]) <= 2 /\ delivered[k] <= produced[k]
=============================================================================
