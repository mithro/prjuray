# Sourced by the equivalence scripts in this directory.
#   W       this checkout (the "new" code)
#   PROD    the build tree with the designs and the production DB
#           (EQ_BUILD, default: the main checkout's build/)
#   S       scratch directory (EQ_SCRATCH, default $PROD/equiv/<checkout>)
#   BASE    git revision of the "old" code (EQ_BASE, default generic-db),
#           exported into $S/old by eq_tree_old
W=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
PROD=${EQ_BUILD:-$(cd "$(git -C "$W" rev-parse --path-format=absolute --git-common-dir)/.." && pwd)/build}
S=${EQ_SCRATCH:-$PROD/equiv/$(basename "$W")}
BASE=${EQ_BASE:-generic-db}
export EQ_SCRATCH=$S
mkdir -p "$S/eq" "$S/prof"

eq_tree_old() {  # (re)exports $BASE into $S/old
    rm -rf "$S/old"
    mkdir -p "$S/old"
    git -C "$W" archive "$BASE" generic | tar -x -C "$S/old"
}
