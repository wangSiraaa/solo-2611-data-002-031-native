# Frozen logs and two bounded event grammars
manifest.json declares exact bytes/SHA256, source format and optional final-block completeness.
Blank lines bound text blocks and are not events. Inside one block, a timestamp-shaped line may be
an event start OR payload continuation. Candidates start with an ISO UTC timestamp and consume 1..max_candidate_lines
contiguous lines, without crossing a block. A candidate's logical message removes only its FIRST timestamp prefix
and its final line terminator; internal timestamp lines and continuation line breaks remain intact.
Use re.fullmatch with DOTALL on that logical message. The rules both recognize candidate events and extract fields.
A complete interpretation covers every line in a block with candidates plus one matched rule per candidate.
No complete interpretation => UNKNOWN block, retain candidate evidence; multiple complete interpretations => ambiguous,
retain at least two complete witnesses. Only a unique complete interpretation yields confirmed text events.
Do not choose locally longest/shortest/first rule. Type conversion follows interpretation; conversion failure stays visible.
JSONL objects are already single-event boundaries, never concatenate objects; match their message fields.
Raw source spans are half-open byte offsets. Final-block completeness false/absent isolates the last text block
(or last JSONL object), independently of a trailing newline; an affirmative declaration does not validate broken syntax.
String fields retain leading zeros; decimals are finite exact decimal text. Fixed error codes remain fixed grammar tokens.
Compare confirmed source byte coverage before comparing field values, so split/merge is not a fake per-ID field change.
Inputs contain several candidate grammars, not annotations or expected cuts. No segmentation/decoder/oracle is supplied.
