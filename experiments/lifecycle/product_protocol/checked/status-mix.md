# status-mix

Actual TLC violation: `ResultHonest`.

| Step/action | Local/remote/gen | Attempt custody | Control | Protocol | Outcome |
| --- | --- | --- | --- | --- | --- |
| 1 Initial predicate |  "opening"/ "created"/ 0 |  << [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<>>; buffer= <<>>; final= FALSE; final-received= FALSE; client= FALSE |  "none"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
| 2 Signal |  "opening"/ "terminating"/ 0 |  << [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<>>; buffer= <<>>; final= FALSE; final-received= FALSE; client= FALSE |  "signal"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
| 3 CommitTerminal |  "opening"/ "terminating"/ 0 |  << [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<"ENDED">>; buffer= <<>>; final= TRUE; final-received= FALSE; client= FALSE |  "signal"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
