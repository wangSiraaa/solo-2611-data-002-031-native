# One stratified household-randomized experiment
All CSV data are synthetic UTF-8. Python standard library suffices; optional statistics packages may be installed.
The plan fixes eligible households before randomization. S1 selects exactly 2 of 6 for treatment;
S2 independently selects exactly 4 of 6. Each eligible subset within a stratum is equally likely.
Membership is fixed for this window. Exposed only describes receipt of treatment and never selects the ITT cohort.
Events outside [window_start, window_end) do not contribute. Amounts are integer cents; refunds would be negative.
Coverage=true explicitly means no missing events for that household during the whole window.
Replace only the corresponding base CSV with a cases/ file for each separate failure scenario.
The data are small and do not promise significance. Search bounds/step are input controls, not expected limits.
No aggregated metric, effect, interval, assignment validity answer or inference code is supplied.
