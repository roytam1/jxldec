#!/usr/bin/env python3
"""Generate a stb-style single-header jxldec.h from dist/jxl.h + dist/jxl.c.

Usage:
    python cmd/build-stb.py [-o OUTPUT]

The output is a drop-in header: declarations are always visible, and defining
JXLDEC_IMPLEMENTATION in exactly one translation unit emits the code.  The
generated file carries the compatibility shims needed by C89 and old MSVC
(VC4/VC6) toolchains; modern compilers are unaffected.

This script deliberately does not touch jxldec/src or dist -- it is a consumer
adapter, the same way j40.h is packaged for picview.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_H = os.path.join(ROOT, "dist", "jxl.h")
DIST_C = os.path.join(ROOT, "dist", "jxl.c")
DEFAULT_OUT = os.path.join(os.path.dirname(ROOT), "jxldec.h")


# ---------------------------------------------------------------------------
# Compatibility prelude (emitted before the public declarations)
# ---------------------------------------------------------------------------
PRELUDE = r"""/* Compatibility layer for the stb-style jxldec.h.  Modern compilers compile
 * this to nothing; it only kicks in for pre-C99 toolchains (C89, VC4/VC6).
 * The typedef/constant block mirrors the one proven in picview's j40.h. */
#ifndef JXLDEC_STB_COMPAT_H
#define JXLDEC_STB_COMPAT_H

#include <stddef.h>
#include <stdarg.h>

#if defined(_MSC_VER) && (_MSC_VER <= 1800)
    typedef unsigned char      uint8_t;
    typedef unsigned short     uint16_t;
    typedef unsigned int       uint32_t;
    typedef unsigned __int64   uint64_t;
    typedef signed char        int8_t;
    typedef signed short       int16_t;
    typedef signed int         int32_t;
    typedef __int64            int64_t;
    #define INT8_MIN   ((int8_t)0x80)
    #define INT8_MAX   ((int8_t)0x7F)
    #define INT16_MIN  ((int16_t)0x8000)
    #define INT16_MAX  ((int16_t)0x7FFF)
    #define INT32_MIN  ((int32_t)0x80000000L)
    #define INT32_MAX  ((int32_t)0x7FFFFFFFL)
    #define UINT32_MAX ((uint32_t)0xFFFFFFFFL)
    #define INT64_MIN  ((int64_t)0x8000000000000000i64)
    #define INT64_MAX  ((int64_t)0x7FFFFFFFFFFFFFFFi64)
    #define UINT64_MAX ((uint64_t)0xFFFFFFFFFFFFFFFFi64)
    #define SIZE_MAX   ((size_t)UINT32_MAX)
    typedef unsigned int       uintptr_t;
    typedef int                intptr_t;
#endif

/* inline is C99; VC4/VC6 have __inline at best, and plain static is always
 * correct.  All internal uses were rewritten to JXL_INLINE by build-stb.py. */
#if defined(_MSC_VER) && _MSC_VER >= 1200
    #define JXL_INLINE __inline
#elif defined(__STDC_VERSION__) && (__STDC_VERSION__ >= 199901L)
    #define JXL_INLINE inline
#elif defined(__GNUC__) || defined(__clang__)
    #define JXL_INLINE __inline__
#else
    #define JXL_INLINE
#endif

/* 64-bit constants: C89 has no ULL suffix, MSVC uses i64. */
#if defined(_MSC_VER) && (_MSC_VER < 1900)
    #define JXL_U64(x) ((uint64_t)(x##i64))
#elif defined(__STDC_VERSION__) && (__STDC_VERSION__ >= 199901L)
    #define JXL_U64(x) ((uint64_t)(x##ull))
#else
    #define JXL_U64(x) ((uint64_t)(x))
#endif

/* C99 <math.h> float variants -> double CRT functions.  Lossy VarDCT pixels
 * may shift by one 8-bit step; compare against the amalgamation when this
 * path is used for a shipping build. */
#if (defined(_MSC_VER) && (_MSC_VER < 1900)) || \
    (!defined(__STDC_VERSION__) || (__STDC_VERSION__ < 199901L))
    #include <math.h>
    #define powf(x, y)   ((float)pow((double)(x), (double)(y)))
    #define sqrtf(x)     ((float)sqrt((double)(x)))
    #define fabsf(x)     ((float)fabs((double)(x)))
    #define logf(x)      ((float)log((double)(x)))
    #define floorf(x)    ((float)floor((double)(x)))
    #define cosf(x)      ((float)cos((double)(x)))
    /* cbrtf must keep the sign: pow(negative, 1/3) would be NaN. */
    #define cbrtf(x)     ((float)((x) < 0 ? -pow(-(double)(x), 1.0 / 3.0) \
                                         : pow((double)(x), 1.0 / 3.0)))
#endif

#if defined(_MSC_VER) && (_MSC_VER < 1900)
    #define vsnprintf _vsnprintf
#endif

/* Old MSVC has neither intrin.h nor the compiler intrinsics used for the
 * cooperative-abort epoch.  Single-threaded Win32s: plain reads/increments
 * are equivalent. */
#if defined(_MSC_VER) && (_MSC_VER < 1400)
    #define _InterlockedCompareExchange(p, a, b) (*(p))
    #define _InterlockedIncrement(p) (++(*(p)))
#endif

/* No SSE2/AVX2 on old MSVC or strict C89: all kernels have bit-identical
 * scalar twins, and old compilers lack the intrinsic headers. */
#if (defined(_MSC_VER) && (_MSC_VER < 1400)) || \
    (!defined(__STDC_VERSION__) || (__STDC_VERSION__ < 199901L))
    #define JXL_NO_AVX2
    #define JXL_COLOR_FORCE_SCALAR
    #define JXL_DCT_FORCE_SCALAR
    #define JXL_EPF_FORCE_SCALAR
    #define JXL_RENDER_FORCE_SCALAR
    #define JXL_SPLINE_FORCE_SCALAR
    #define JXL_UPSAMPLE_FORCE_SCALAR
    #define JXL_VARDCT_FORCE_SCALAR
    #define JXL_NOISE_FORCE_SCALAR
#endif

#endif /* JXLDEC_STB_COMPAT_H */

"""


# ---------------------------------------------------------------------------
# Text transformations applied to the amalgamated implementation
# ---------------------------------------------------------------------------
def rewrite_error_calls(text):
    """Turn JXL_ERR/JXL_WARN(ctx, fmt, ...) into direct jxl_errorf calls.

    Variadic macros are a C99 feature absent from C89 and old MSVC.  jxl_errorf
    is an ordinary variadic *function*, so inserting the severity argument
    after the first top-level comma removes the need for them entirely.
    String literals and nested parens are skipped; formatting is preserved.
    """
    # Remove the macro definitions themselves before scanning for call sites.
    text = re.sub(r"^[ \t]*#define JXL_ERR[^\n]*\n", "", text, flags=re.M)
    text = re.sub(r"^[ \t]*#define JXL_WARN[^\n]*\n", "", text, flags=re.M)

    names = {"JXL_ERR": "JXLDEC_SEVERITY_ERROR",
             "JXL_WARN": "JXLDEC_SEVERITY_WARNING"}
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue

        matched = None
        for name in names:
            if text.startswith(name, i):
                j = i + len(name)
                if j < n and text[j] == "(":
                    matched = name
                    break
        if matched is not None:
            depth = 1
            k = i + len(matched) + 1
            comma = None
            while k < n and depth > 0:
                ch = text[k]
                if ch in ('"', "'"):
                    q = ch
                    k += 1
                    while k < n:
                        if text[k] == "\\" and k + 1 < n:
                            k += 2
                            continue
                        if text[k] == q:
                            k += 1
                            break
                        k += 1
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif ch == "," and depth == 1 and comma is None:
                    comma = k
                k += 1
            if comma is not None:
                out.append("jxl_errorf")
                out.append(text[i + len(matched):comma + 1])
                out.append(" " + names[matched] + ",")
                i = comma + 1
                continue

        out.append(c)
        i += 1
    return "".join(out)


def rename_identifier(text, old, new):
    """Rename an identifier token everywhere except inside string/char literals.

    windows.h pulls in rpcndr.h, which does `#define small char` on old MSVC.
    jxldec uses `small` as a local variable name, so preprocess-time macro
    expansion corrupts it; rename it to a collision-free internal name.
    """
    out = []
    i, n = 0, len(text)
    pat = re.compile(r"\b" + re.escape(old) + r"\b")
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        m = pat.match(text, i)
        if m:
            out.append(new)
            i = m.end()
            continue
        out.append(c)
        i += 1
    return "".join(out)


def transform_impl(text):
    text = rewrite_error_calls(text)

    # Avoid the Windows `small` -> `char` macro collision (rpcndr.h).
    text = rename_identifier(text, "small", "jxl_small")

    text = re.sub(r"\bstatic\s+inline\b", "JXL_INLINE", text)

    # JXL_INLINE_HINT expands to C99 `inline` on gcc/clang; fall back to the
    # same shim used for plain inline on pre-C99 toolchains.
    text = text.replace(
        "#if defined(_MSC_VER) && !defined(__clang__)\n"
        "#define JXL_INLINE_HINT __forceinline\n"
        "#elif defined(__clang__) || defined(__GNUC__)\n"
        "#define JXL_INLINE_HINT inline __attribute__((always_inline))\n"
        "#else\n"
        "#define JXL_INLINE_HINT\n"
        "#endif",
        "#if defined(_MSC_VER) && !defined(__clang__) && _MSC_VER >= 1400\n"
        "#define JXL_INLINE_HINT __forceinline\n"
        "#elif (defined(__clang__) || defined(__GNUC__)) && \\\n"
        "      defined(__STDC_VERSION__) && __STDC_VERSION__ >= 199901L\n"
        "#define JXL_INLINE_HINT inline __attribute__((always_inline))\n"
        "#else\n"
        "#define JXL_INLINE_HINT JXL_INLINE\n"
        "#endif")

    # C99 allows aggregate initializers computed at runtime; C89 does not.
    text = text.replace(
        "jxl_bytecur hdr = {enc, header_len, 0, 0};",
        "jxl_bytecur hdr;\n    hdr.p = enc; hdr.len = header_len; "
        "hdr.pos = 0; hdr.err = 0;")
    text = text.replace(
        "jxl_bytecur hdr = {stream, stream_len, 0, 0};",
        "jxl_bytecur hdr;\n    hdr.p = stream; hdr.len = stream_len; "
        "hdr.pos = 0; hdr.err = 0;")
    text = text.replace(
        "const ptrdiff_t koff[4] = {-(ptrdiff_t)stride, (ptrdiff_t)stride, -1, 1};",
        "ptrdiff_t koff[4];\n    koff[0] = -(ptrdiff_t)stride; "
        "koff[1] = (ptrdiff_t)stride; koff[2] = -1; koff[3] = 1;")

    # C99 allows declarations after statements; C89 requires block tops.
    def hoist(old, new):
        nonlocal text
        if old not in text:
            raise RuntimeError("C89 hoist pattern not found: %r" % old[:60])
        text = text.replace(old, new, 1)

    hoist(
        "static int jxl_detect_avx2_fma(void) {\n"
        "    if (!jxl_has_avx2()) return 0;\n"
        "#if defined(_WIN32) && (defined(_M_X64) || defined(_M_AMD64) || defined(_M_IX86))\n"
        "    int r[4];\n",
        "static int jxl_detect_avx2_fma(void) {\n"
        "    int r[4];\n"
        "    if (!jxl_has_avx2()) return 0;\n"
        "#if defined(_WIN32) && (defined(_M_X64) || defined(_M_AMD64) || defined(_M_IX86))\n")
    hoist(
        "    if (!all_default) {\n"
        "        extra_fields = jxl_br_bool(br);\n",
        "    if (!all_default) {\n"
        "        uint32_t n_extra;\n"
        "        extra_fields = jxl_br_bool(br);\n")
    hoist(
        "        uint32_t n_extra = jxl_br_u32(br, 0, 0, 1, 0, 2, 4, 1, 12);\n",
        "        n_extra = jxl_br_u32(br, 0, 0, 1, 0, 2, 4, 1, 12);\n")
    hoist(
        "    if (header_len > 0) {\n"
        "        jxl_bytecur hdr;\n"
        "    hdr.p = enc; hdr.len = header_len; hdr.pos = 0; hdr.err = 0;\n"
        "        uint64_t output_size = bc_varint(&hdr);\n"
        "        uint64_t commands_size = bc_varint(&hdr);\n"
        "        size_t stream_offset = hdr.pos;\n",
        "    if (header_len > 0) {\n"
        "        jxl_bytecur hdr;\n"
        "        uint64_t output_size, commands_size;\n"
        "        size_t stream_offset;\n"
        "        hdr.p = enc; hdr.len = header_len; hdr.pos = 0; hdr.err = 0;\n"
        "        output_size = bc_varint(&hdr);\n"
        "        commands_size = bc_varint(&hdr);\n"
        "        stream_offset = hdr.pos;\n")
    hoist(
        "    jxl_bytecur hdr;\n"
        "    hdr.p = stream; hdr.len = stream_len; hdr.pos = 0; hdr.err = 0;\n"
        "    jxl_bytecur cmds;\n"
        "    uint64_t output_size, commands_size;\n"
        "    size_t stream_offset;\n"
        "    const uint8_t *data;\n"
        "    size_t data_len;\n"
        "    size_t header_size;\n"
        "    jxl_bytebuf out;\n"
        "    uint8_t *shuf = NULL;\n"
        "    size_t i;\n"
        "    int rc = -1;\n",
        "    jxl_bytecur hdr, cmds;\n"
        "    uint64_t output_size, commands_size;\n"
        "    size_t stream_offset, data_len, header_size, i;\n"
        "    const uint8_t *data;\n"
        "    jxl_bytebuf out;\n"
        "    uint8_t *shuf = NULL;\n"
        "    int rc = -1;\n"
        "    hdr.p = stream; hdr.len = stream_len; hdr.pos = 0; hdr.err = 0;\n")
    hoist(
        "    if (err_sum <= 62)\n"
        "        return 4 + wn * dl[err_sum + 1];\n"
        "    uint64_t v = ((uint64_t)err_sum + 1) >> 5;\n"
        "    uint32_t shift = v > 1 ? jxl_floor_log2_u64(v) : 0;\n"
        "    uint32_t idx = (err_sum >> shift) + 1;\n",
        "    uint64_t v;\n"
        "    uint32_t shift, idx;\n"
        "    if (err_sum <= 62)\n"
        "        return 4 + wn * dl[err_sum + 1];\n"
        "    v = ((uint64_t)err_sum + 1) >> 5;\n"
        "    shift = v > 1 ? jxl_floor_log2_u64(v) : 0;\n"
        "    idx = (err_sum >> shift) + 1;\n")
    hoist(
        "            uint32_t lg_x = lf_per_row ? i % lf_per_row : 0;\n"
        "            if (jxl_aborted(ctx)) goto done;\n"
        "            uint32_t lg_y = lf_per_row ? i / lf_per_row : 0;\n",
        "            uint32_t lg_x = lf_per_row ? i % lf_per_row : 0;\n"
        "            uint32_t lg_y;\n"
        "            if (jxl_aborted(ctx)) goto done;\n"
        "            lg_y = lf_per_row ? i / lf_per_row : 0;\n")
    hoist(
        "            if (!(bi->hf_nonzero_mask & (1u << channel))) continue;\n"
        "            float mul =\n",
        "            float mul;\n"
        "            if (!(bi->hf_nonzero_mask & (1u << channel))) continue;\n"
        "            mul =\n")
    hoist(
        "            float *dst = plane[c] + (size_t)y * stride;\n"
        "\n"
        "            memcpy(cur, dst, (size_t)w * sizeof(float));\n"
        "            rc = cur;\n"
        "            rn = y > 0 ? ring + (size_t)((y - 1) & 1u) * w : cur;\n"
        "            rs = y + 1 < h ? plane[c] + (size_t)(y + 1) * stride : cur;\n"
        "            uint32_t xlo = w > 1 ? 1 : 0, xhi = w > 1 ? w - 1 : 0;\n",
        "            float *dst = plane[c] + (size_t)y * stride;\n"
        "            uint32_t xlo, xhi;\n"
        "\n"
        "            memcpy(cur, dst, (size_t)w * sizeof(float));\n"
        "            rc = cur;\n"
        "            rn = y > 0 ? ring + (size_t)((y - 1) & 1u) * w : cur;\n"
        "            rs = y + 1 < h ? plane[c] + (size_t)(y + 1) * stride : cur;\n"
        "            xlo = w > 1 ? 1 : 0; xhi = w > 1 ? w - 1 : 0;\n")
    hoist(
        "            uint32_t lg_x = lf_per_row ? i % lf_per_row : 0;\n"
        "            uint32_t lg_y;\n"
        "            if (jxl_aborted(ctx)) goto done;\n"
        "            lg_y = lf_per_row ? i / lf_per_row : 0;\n"
        "\n"
        "            uint32_t blocks_w = jxl_frame_blocks_w(fh);\n"
        "            uint32_t blocks_h = jxl_frame_blocks_h(fh);\n"
        "            uint32_t base_bx = lg_x * group_dim;\n"
        "            uint32_t base_by = lg_y * group_dim;\n"
        "            uint32_t lf_w = JXL_MIN(group_dim, blocks_w - base_bx) * 8;\n"
        "            uint32_t lf_h = JXL_MIN(group_dim, blocks_h - base_by) * 8;\n",
        "            uint32_t lg_x, lg_y, blocks_w, blocks_h, base_bx, base_by;\n"
        "            uint32_t lf_w, lf_h;\n"
        "            lg_x = lf_per_row ? i % lf_per_row : 0;\n"
        "            if (jxl_aborted(ctx)) goto done;\n"
        "            lg_y = lf_per_row ? i / lf_per_row : 0;\n"
        "\n"
        "            blocks_w = jxl_frame_blocks_w(fh);\n"
        "            blocks_h = jxl_frame_blocks_h(fh);\n"
        "            base_bx = lg_x * group_dim;\n"
        "            base_by = lg_y * group_dim;\n"
        "            lf_w = JXL_MIN(group_dim, blocks_w - base_bx) * 8;\n"
        "            lf_h = JXL_MIN(group_dim, blocks_h - base_by) * 8;\n")

    # A bare 64-bit literal is not valid C89; split the PRNG constants into
    # two 32-bit halves before the generic suffix rewrite.
    for big, split in (
        ("0xBF58476D1CE4E5B9", "(((uint64_t)0xBF58476D) << 32 | 0x1CE4E5B9)"),
        ("0x94D049BB133111EB", "(((uint64_t)0x94D049BB) << 32 | 0x133111EB)"),
        ("0x9E3779B97F4A7C15", "(((uint64_t)0x9E3779B9) << 32 | 0x7F4A7C15)"),
    ):
        text = text.replace(big + "ull", split)

    text = re.sub(r"\b(0[xX][0-9A-Fa-f]+|[0-9]+)ull\b", r"JXL_U64(\1)", text)

    # _BitScanReverse is a VC7+ intrinsic; fall through to the portable loop
    # on older compilers.  __cpuidex is an intrinsic too, so skip the FMA
    # probe when AVX2 is disabled at compile time.
    hoist(
        "#elif defined(_MSC_VER) && !defined(__clang__)\n"
        "\n"
        "    unsigned long idx;\n",
        "#elif defined(_MSC_VER) && !defined(__clang__) && _MSC_VER >= 1400\n"
        "\n"
        "    unsigned long idx;\n")
    hoist(
        "    if (!jxl_has_avx2()) return 0;\n"
        "#if defined(_WIN32) && (defined(_M_X64) || defined(_M_AMD64) || defined(_M_IX86))\n"
        "    __cpuidex(r, 1, 0);\n",
        "    if (!jxl_has_avx2()) return 0;\n"
        "#if !defined(JXL_NO_AVX2) && defined(_WIN32) && \\\n"
        "    (defined(_M_X64) || defined(_M_AMD64) || defined(_M_IX86))\n"
        "    __cpuidex(r, 1, 0);\n")
    hoist(
        "static int jxl_detect_avx2_fma(void) {\n"
        "    int r[4];\n",
        "static int jxl_detect_avx2_fma(void) {\n"
        "    int r[4];\n"
        "    unsigned a, b, c, d;\n")
    hoist(
        "#elif defined(__x86_64__) || defined(__i386__)\n"
        "    unsigned a, b, c, d;\n"
        "    if (!__get_cpuid_count(1, 0, &a, &b, &c, &d)) return 0;\n"
        "    return (c & (1u << 12)) != 0;\n",
        "#elif defined(__x86_64__) || defined(__i386__)\n"
        "    if (!__get_cpuid_count(1, 0, &a, &b, &c, &d)) return 0;\n"
        "    return (c & (1u << 12)) != 0;\n")

    # intrin.h does not exist on VC4; let the epoch code use the plain Win32
    # macros from the prelude instead.
    text = text.replace(
        "#if defined(_WIN32)\n#include <intrin.h>",
        "#if defined(_WIN32) && !(defined(_MSC_VER) && _MSC_VER < 1400)\n"
        "#include <intrin.h>")
    text = text.replace(
        "#if defined(_MSC_VER) && !defined(__clang__)\n#include <intrin.h>",
        "#if defined(_MSC_VER) && !defined(__clang__)\n"
        "#if _MSC_VER >= 1400\n"
        "#include <intrin.h>\n"
        "#endif")
    # VC6 does not know __declspec(noinline); fall through to the empty
    # branch of these macro definitions instead.
    for name in ("JXL_MODULAR_NOINLINE", "JXL_EPF_NOINLINE"):
        text = text.replace(
            "#if defined(_MSC_VER)\n"
            "#define %s __declspec(noinline)" % name,
            "#if defined(_MSC_VER) && _MSC_VER >= 1400\n"
            "#define %s __declspec(noinline)" % name)
    return text


def build(out_path):
    with open(DIST_H, "r", encoding="utf-8") as f:
        public = f.read().replace("\r\n", "\n")
    # Old MSVC has no <stdint.h>; the prelude already supplies the typedefs.
    public = public.replace(
        "#include <stdint.h>",
        "#if !defined(_MSC_VER) || (_MSC_VER >= 1900)\n"
        "#include <stdint.h>\n"
        "#endif")
    with open(DIST_C, "r", encoding="utf-8") as f:
        impl = f.read().replace("\r\n", "\n")

    impl = transform_impl(impl)

    header = (
        "/* jxldec.h -- stb-style single-header JPEG XL decoder.\n"
        " * Generated by cmd/build-stb.py from dist/jxl.h + dist/jxl.c.\n"
        " * Define JXLDEC_IMPLEMENTATION in exactly ONE source file.\n"
        " */\n\n"
        + PRELUDE
        + public.rstrip() + "\n\n"
        + "#ifdef JXLDEC_IMPLEMENTATION\n\n"
        + impl.rstrip() + "\n\n"
        + "#endif /* JXLDEC_IMPLEMENTATION */\n"
    )
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
    print("wrote %s (%d lines)" % (out_path, header.count("\n") + 1))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=DEFAULT_OUT,
                    help="output header (default: ../jxldec.h)")
    args = ap.parse_args()
    if not os.path.exists(DIST_H) or not os.path.exists(DIST_C):
        sys.exit("missing dist/jxl.h or dist/jxl.c -- run build-dist.ts first")
    build(os.path.abspath(args.output))


if __name__ == "__main__":
    main()
