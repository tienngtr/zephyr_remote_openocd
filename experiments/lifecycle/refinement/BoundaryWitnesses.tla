-------------------------- MODULE BoundaryWitnesses --------------------------
\* SPDX-License-Identifier: Apache-2.0
EXTENDS BoundaryProtocol

\* Stronger coverage targets; the protocol and its transitions are unchanged.
NoTerminalEnqueuedWriterWitness ==
    ~(st.audit.terminalWriterFailed /\ st.primary.code = "operational"
      /\ st.stage[1] = "settled" /\ st.cleanupActor[1] # None
      /\ Len(st.journal) \in {st.admittedOutput[i] : i \in 1..Len(st.admittedOutput)})

NoLateOfferWitness ==
    ~(st.intent = "STOP" /\ st.audit.lateAcquired /\ st.stage[1] = "offered"
      /\ st.offers = <<>> /\ st.owner[1] = "producer")

NoLostAckPendingWitness ==
    ~(st.intent = "STOP" /\ st.ackLost[1] /\ st.audit.ackLostBeforeTerminal
      /\ ~st.released[1] /\ st.resource[1] /\ st.owner[1] = "supervisor")

NoResidualWitness ==
    ~(st.phase = "Closed" /\ st.residual[1] /\ st.resource[1]
      /\ st.owner[1] = "supervisor" /\ st.primary.code = "operational"
      /\ Error("cleanup", 1) \in {st.diagnostics[i] : i \in 1..Len(st.diagnostics)})

NoWriterRetryBlockWitness ==
    ~(SuccessEligible("retry") /\ st.writerPending /\ OldSettled /\ ~Accounted)
=============================================================================
