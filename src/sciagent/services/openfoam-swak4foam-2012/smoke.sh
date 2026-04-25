#!/usr/bin/env bash
# Smoke test for openfoam-swak4foam-2012.
#
# Exercises every regression vector this image exists to fix, in a single
# decomposePar pass so the test catches both build-time and load-time issues:
#
#   1. libmetisDecomp.so exports real Foam::metisDecomp symbols (not the stub)
#   2. groovyBC valueExpression with $var dict-lookup AND {patch_outlet}
#      patch-data lookup (the v2406 dictionary::csearch ABI segfault path)
#   3. outletMappedUniformInletHeatAddition is a registered BC (removed in v2406)
#   4. omegaWallFunction with "blending binomial2" parses (renamed in v2406)
#   5. method metis in decomposeParDict actually decomposes
#   6. All four buoyant solvers used by the case suite link cleanly
#
# Runs both during image build (Dockerfile RUN) and as a CI step against the
# built image before push.

# OpenFOAM's etc/bashrc invokes internal helpers (_foamAddLibAuto, ...) that
# `return 1` as normal control flow when an optional ThirdParty package is
# absent. With `set -e` active during sourcing, those returns kill the shell.
# So we source first WITHOUT strict mode, then enable it.
source /usr/lib/openfoam/openfoam2012/etc/bashrc
set -euo pipefail

export FOAM_USER_LIBBIN="$FOAM_SITE_LIBBIN"
export FOAM_USER_APPBIN="$FOAM_SITE_APPBIN"

say() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# -- 1. Required utilities on PATH ----------------------------------------
say "PATH check"
for tool in funkySetFields funkyDoCalc blockMesh snappyHexMesh decomposePar \
            buoyantSimpleFoam buoyantBoussinesqSimpleFoam \
            buoyantPimpleFoam buoyantBoussinesqPimpleFoam; do
  if ! command -v "$tool" >/dev/null; then
    echo "--- swak4foam_build1.log tail ---"; tail -200 /tmp/swak4foam_build1.log 2>/dev/null || true
    echo "--- swak4foam_build2.log tail ---"; tail -200 /tmp/swak4foam_build2.log 2>/dev/null || true
    fail "$tool not on PATH"
  fi
done
echo "OK: utilities present"

# -- 2. Real metisDecomp (not the stub) ----------------------------------
# Both stub and real libs export the same Foam::metisDecomp symbols, so an
# nm check cannot distinguish them. The discriminating signal is whether
# libmetisDecomp.so depends on libmetis.so (only the real rebuild does)
# and whether it lives at $FOAM_LIBBIN/ (real) vs $FOAM_LIBBIN/dummy/ (stub).
say "metisDecomp lib check (must be the real rebuild, not the dummy stub)"
LIB="$FOAM_LIBBIN/libmetisDecomp.so"
[ -f "$LIB" ] || fail "$LIB missing (only the dummy stub at $FOAM_LIBBIN/dummy/ is present)"
if ! ldd "$LIB" | grep -q 'libmetis\.so'; then
  echo "--- ldd $LIB ---"; ldd "$LIB"
  fail "$LIB does not link against libmetis (still effectively a stub)"
fi
echo "OK: real metisDecomp at $LIB, links libmetis"

# -- 3. Linker check on the four buoyant solvers --------------------------
say "buoyant solver -help check"
for s in buoyantSimpleFoam buoyantBoussinesqSimpleFoam \
         buoyantPimpleFoam buoyantBoussinesqPimpleFoam; do
  "$s" -help >/dev/null || fail "$s -help failed (likely a missing/incompatible library)"
done
echo "OK: all four buoyant solvers load"

# -- 4. Combined-BC + metis case ------------------------------------------
# A 4x2x2 box mesh with two pairs of named patches mimicking the rack
# topology of the data-center cases. 0/T puts a groovyBC on rack01_inlet
# (with $var lookup AND {patch_outlet} cross-patch lookup, exactly as in the
# real cases) and an outletMappedUniformInletHeatAddition on rack02_inlet.
# 0/omega uses the binomial2 blending. decomposePar then constructs every BC
# under method=metis -- one pass exercises all four regression vectors.
say "combined-BC + metis decomposePar"
CASE="$(mktemp -d)"
trap 'rm -rf "$CASE"' EXIT

mkdir -p "$CASE/0" "$CASE/constant" "$CASE/system"

cat >"$CASE/system/controlDict" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application     buoyantBoussinesqSimpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
purgeWrite      0;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;
libs ("libgroovyBC.so" "libswakFunctionObjects.so");
EOF

cat >"$CASE/system/blockMeshDict" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object blockMeshDict; }
scale 1;
vertices
(
    (0 0 0) (1 0 0) (1 1 0) (0 1 0)
    (0 0 1) (1 0 1) (1 1 1) (0 1 1)
);
blocks
(
    hex (0 1 2 3 4 5 6 7) (4 4 4) simpleGrading (1 1 1)
);
edges ();
boundary
(
    rack01_inlet  { type patch; faces ((0 4 7 3)); }   // xmin
    rack01_outlet { type patch; faces ((1 2 6 5)); }   // xmax
    rack02_inlet  { type patch; faces ((0 1 5 4)); }   // ymin
    rack02_outlet { type patch; faces ((3 7 6 2)); }   // ymax
    walls
    {
        type wall;
        faces ((0 3 2 1) (4 5 6 7));                   // zmin + zmax
    }
);
mergePatchPairs ();
EOF

cat >"$CASE/system/decomposeParDict" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains 2;
method metis;
EOF

cat >"$CASE/system/fvSchemes" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes      { default steadyState; }
gradSchemes     { default Gauss linear; }
divSchemes      { default none; div(phi,U) bounded Gauss linearUpwind grad(U); div(phi,T) bounded Gauss linearUpwind grad(T); div(phi,k) bounded Gauss linearUpwind grad(k); div(phi,omega) bounded Gauss linearUpwind grad(omega); div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes   { default corrected; }
EOF

cat >"$CASE/system/fvSolution" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers { "(p_rgh|U|T|k|omega)" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-6; relTol 0.1; } }
SIMPLE { nNonOrthogonalCorrectors 0; pRefCell 0; pRefValue 0; }
EOF

cat >"$CASE/constant/turbulenceProperties" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object turbulenceProperties; }
simulationType RAS;
RAS { RASModel kOmegaSST; turbulence on; printCoeffs on; }
EOF

cat >"$CASE/constant/transportProperties" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object transportProperties; }
transportModel  Newtonian;
nu              1.5e-5;
beta            3.3e-3;
TRef            300;
Pr              0.7;
Prt             0.85;
EOF

cat >"$CASE/constant/g" <<'EOF'
FoamFile { version 2.0; format ascii; class uniformDimensionedVectorField; object g; }
dimensions [0 1 -2 0 0 0 0];
value (0 0 -9.81);
EOF

# 0/T -- the gauntlet:
#   * top-of-file dict entries Cp / PR1 / QR1 (parse-time expansion)
#   * groovyBC valueExpression "$PR1/($Cp*$QR1)"  -> dict csearch ABI path
#   * variables "T_return{rack01_outlet}=..."     -> patch-data lookup ABI
#   * outletMappedUniformInletHeatAddition on rack02_inlet (v2012-only BC)
cat >"$CASE/0/T" <<'EOF'
FoamFile { version 2.0; format ascii; class volScalarField; object T; }
dimensions [0 0 0 1 0 0 0];
Cp  1006;
PR1 6208;
QR1 1.190;
internalField uniform 300;
boundaryField
{
    rack01_inlet
    {
        type            groovyBC;
        valueExpression "T_return + $PR1/($Cp*$QR1)";
        variables       "T_return{rack01_outlet}=sum(T*mag(Sf()))/sum(mag(Sf()));";
        value           uniform 300;
    }
    rack01_outlet { type zeroGradient; }
    rack02_inlet
    {
        type            outletMappedUniformInletHeatAddition;
        outletPatch     rack02_outlet;
        Q               7481;
        phi             phi;
        value           uniform 300;
    }
    rack02_outlet { type zeroGradient; }
    walls         { type zeroGradient; }
}
EOF

cat >"$CASE/0/U" <<'EOF'
FoamFile { version 2.0; format ascii; class volVectorField; object U; }
dimensions [0 1 -1 0 0 0 0];
internalField uniform (0 0 0);
boundaryField
{
    rack01_inlet  { type fixedValue; value uniform (1 0 0); }
    rack01_outlet { type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }
    rack02_inlet  { type fixedValue; value uniform (1 0 0); }
    rack02_outlet { type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }
    walls         { type noSlip; }
}
EOF

cat >"$CASE/0/p_rgh" <<'EOF'
FoamFile { version 2.0; format ascii; class volScalarField; object p_rgh; }
dimensions [1 -1 -2 0 0 0 0];
internalField uniform 0;
boundaryField
{
    rack01_inlet  { type fixedFluxPressure; value uniform 0; }
    rack01_outlet { type prghPressure; p uniform 0; value uniform 0; }
    rack02_inlet  { type fixedFluxPressure; value uniform 0; }
    rack02_outlet { type prghPressure; p uniform 0; value uniform 0; }
    walls         { type fixedFluxPressure; value uniform 0; }
}
EOF

cat >"$CASE/0/p" <<'EOF'
FoamFile { version 2.0; format ascii; class volScalarField; object p; }
dimensions [1 -1 -2 0 0 0 0];
internalField uniform 0;
boundaryField
{
    rack01_inlet  { type calculated; value uniform 0; }
    rack01_outlet { type calculated; value uniform 0; }
    rack02_inlet  { type calculated; value uniform 0; }
    rack02_outlet { type calculated; value uniform 0; }
    walls         { type calculated; value uniform 0; }
}
EOF

cat >"$CASE/0/k" <<'EOF'
FoamFile { version 2.0; format ascii; class volScalarField; object k; }
dimensions [0 2 -2 0 0 0 0];
internalField uniform 0.1;
boundaryField
{
    rack01_inlet  { type fixedValue; value uniform 0.1; }
    rack01_outlet { type inletOutlet; inletValue uniform 0.1; value uniform 0.1; }
    rack02_inlet  { type fixedValue; value uniform 0.1; }
    rack02_outlet { type inletOutlet; inletValue uniform 0.1; value uniform 0.1; }
    walls         { type kqRWallFunction; value uniform 0.1; }
}
EOF

# 0/omega -- "blending binomial2" is the v2012 spelling that became
# "binomial" in v2406. Parsing this dict is the regression check.
cat >"$CASE/0/omega" <<'EOF'
FoamFile { version 2.0; format ascii; class volScalarField; object omega; }
dimensions [0 0 -1 0 0 0 0];
internalField uniform 1;
boundaryField
{
    rack01_inlet  { type fixedValue; value uniform 1; }
    rack01_outlet { type inletOutlet; inletValue uniform 1; value uniform 1; }
    rack02_inlet  { type fixedValue; value uniform 1; }
    rack02_outlet { type inletOutlet; inletValue uniform 1; value uniform 1; }
    walls
    {
        type     omegaWallFunction;
        blending binomial2;
        value    uniform 1;
    }
}
EOF

cat >"$CASE/0/nut" <<'EOF'
FoamFile { version 2.0; format ascii; class volScalarField; object nut; }
dimensions [0 2 -1 0 0 0 0];
internalField uniform 0;
boundaryField
{
    rack01_inlet  { type calculated; value uniform 0; }
    rack01_outlet { type calculated; value uniform 0; }
    rack02_inlet  { type calculated; value uniform 0; }
    rack02_outlet { type calculated; value uniform 0; }
    walls         { type nutkWallFunction; value uniform 0; }
}
EOF

cd "$CASE"
blockMesh > /tmp/smoke_blockMesh.log 2>&1 \
    || { tail -100 /tmp/smoke_blockMesh.log; fail "blockMesh failed"; }

# decomposePar reads every 0/ field, constructs every BC, and uses the metis
# decomposer. One command, all four regression vectors:
#   * method metis              -- real libmetisDecomp.so (not the stub)
#   * groovyBC                  -- $var dict-csearch + {patch_outlet} lookup
#   * outletMappedUniformInletHeatAddition  -- registered class (v2012-only)
#   * omegaWallFunction binomial2  -- v2012 spelling parses cleanly
decomposePar -force > /tmp/smoke_decompose.log 2>&1 \
    || { tail -200 /tmp/smoke_decompose.log; fail "decomposePar (method metis + groovyBC + outletMappedUniformInletHeatAddition + omegaWallFunction binomial2) failed"; }

# Confirm metis actually produced two subdomains.
[ -d processor0 ] && [ -d processor1 ] \
    || fail "decomposePar did not produce processor0/ and processor1/"

echo
echo "ALL CHECKS PASSED"
