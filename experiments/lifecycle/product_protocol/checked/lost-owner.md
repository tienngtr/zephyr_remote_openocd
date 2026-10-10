# lost-owner

Actual TLC violation: `ResourceOwned`.

| Step/action | Local/remote/gen | Attempt custody | Control | Protocol | Outcome |
| --- | --- | --- | --- | --- | --- |
| 1 Initial predicate |  "opening"/ "created"/ 0 |  << [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<>>; buffer= <<>>; final= FALSE; final-received= FALSE; client= FALSE |  "none"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
| 2 Start |  "opening"/ "starting"/ 1 |  << [stage &#124;-> "authorized", live &#124;-> FALSE, owner &#124;-> "producer"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<>>; buffer= <<>>; final= FALSE; final-received= FALSE; client= FALSE |  "none"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
| 3 Dispatch |  "opening"/ "starting"/ 1 |  << [stage &#124;-> "producing", live &#124;-> FALSE, owner &#124;-> "producer"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<"ATTEMPT">>; buffer= <<"ATTEMPT">>; final= FALSE; final-received= FALSE; client= FALSE |  "none"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
| 4 Acquire |  "opening"/ "starting"/ 1 |  << [stage &#124;-> "owned", live &#124;-> TRUE, owner &#124;-> "none"], [stage &#124;-> "absent", live &#124;-> FALSE, owner &#124;-> "none"] >> |  "none" | committed= <<"ATTEMPT">>; buffer= <<"ATTEMPT">>; final= FALSE; final-received= FALSE; client= FALSE |  "none"; child= <<>>; primary= "none"; diagnostics= <<>>; local-primary= "none"; local-diagnostics= <<>> |
