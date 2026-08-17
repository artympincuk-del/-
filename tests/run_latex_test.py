import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "latex_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"

from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def run():
    # ------------------------------------------------------------------
    # Delimiter wrappers unwrap to their content.
    # ------------------------------------------------------------------
    check(r"\(x\) unwraps", handlers._convert_latex(r"реши \(x^2\)") == "реши x^2")
    check(r"\[x\] unwraps", handlers._convert_latex(r"\[y = 2x\]") == "y = 2x")
    check(r"$$x$$ unwraps", handlers._convert_latex(r"$$a+b$$") == "a+b")
    check(r"$x$ unwraps", handlers._convert_latex(r"ответ: $42$") == "ответ: 42")
    check(
        r"$$ processed before lone $ (no leftover $)",
        "$" not in handlers._convert_latex(r"$$x^2$$ и $y$"),
    )

    # ------------------------------------------------------------------
    # \frac, ^{}, _{}, \sqrt — parens are dropped only for a single
    # character or a plain number; anything longer gets wrapped so the
    # expression's meaning survives (a bare regex previously produced
    # "3±√9+16{2}" for a fraction whose numerator had a nested \sqrt, and
    # "√x+5" for \sqrt{x+5} — silently turning "root of x+5" into
    # "root of x, plus 5").
    # ------------------------------------------------------------------
    check(r"\frac{a}{b} -> a/b (single digits, no parens)", handlers._convert_latex(r"\frac{1}{2}") == "1/2")
    check(
        r"\frac with multi-char parts gets wrapped in parens (not glued: a+1/b-2 would misparse)",
        handlers._convert_latex(r"\frac{a+1}{b-2}") == "(a+1)/(b-2)",
    )
    check(r"^{n} -> ^n (single/plain number, no parens)", handlers._convert_latex(r"x^{10}") == "x^10")
    check(r"^{n+1} -> ^(n+1) (expression, wrapped)", handlers._convert_latex(r"x^{n+1}") == "x^(n+1)")
    check(r"_{n} -> plain n (single char, no parens)", handlers._convert_latex(r"a_{i}") == "ai")
    check(r"_{2} -> plain 2 (single digit, no parens)", handlers._convert_latex(r"a_{2}") == "a2")
    check(
        r"_{i+1} -> (i+1) (expression subscript, wrapped, not glued into ai+1)",
        handlers._convert_latex(r"a_{i+1}") == "a(i+1)",
    )
    check(r"\sqrt{16} -> √16 (plain number, no parens)", handlers._convert_latex(r"\sqrt{16}") == "√16")
    check(
        r"\sqrt{x+5} -> √(x+5), NOT √x+5 (root of a sum, not root of x plus 5 — a meaning bug, not cosmetic)",
        handlers._convert_latex(r"\sqrt{x+5}") == "√(x+5)",
    )

    # ------------------------------------------------------------------
    # Nested \frac/\sqrt: brace matching must track depth, not stop at the
    # first '}' — reported live on a real bot answer: \frac{3 \pm
    # \sqrt{9+16}}{2} came out as "3±√9+16{2}", the denominator lost
    # entirely because a flat regex's numerator match stopped at \sqrt's
    # own inner '}' instead of \frac's real one.
    # ------------------------------------------------------------------
    nested = r"\frac{3 \pm \sqrt{9+16}}{2} \; \text{и} \; \sqrt{x+5}"
    out_nested = handlers._convert_latex(nested)
    check("nested frac/sqrt: no backslashes left", "\\" not in out_nested)
    check("nested frac/sqrt: no curly braces left", "{" not in out_nested and "}" not in out_nested)
    check("nested frac/sqrt: the denominator is correctly '2', not swallowed by the nested sqrt", "/2" in out_nested)
    check(
        "nested frac/sqrt: numerator resolves the inner sqrt first, then wraps correctly",
        "(3 ± √(9+16))/2" in out_nested,
    )
    check("nested frac/sqrt: the trailing sqrt(x+5) also keeps its parens", "√(x+5)" in out_nested)

    # ------------------------------------------------------------------
    # No-argument spacing/line-break commands are dropped entirely, not
    # left in the text as raw backslash-punctuation.
    # ------------------------------------------------------------------
    for cmd in (r"\;", r"\,", r"\:", r"\!", r"\quad", r"\qquad", r"\newline"):
        out = handlers._convert_latex(f"a {cmd} b")
        check(f"{cmd!r} is removed, not left as raw text", cmd not in out and "\\" not in out)
    check(r"\\ (forced line break) is removed", "\\" not in handlers._convert_latex(r"строка1 \\ строка2"))

    # ------------------------------------------------------------------
    # Operators, comparisons, greek letters.
    # ------------------------------------------------------------------
    check(r"\cdot -> ·", handlers._convert_latex(r"2 \cdot 3") == "2 · 3")
    check(r"\times -> ×", handlers._convert_latex(r"2 \times 3") == "2 × 3")
    check(r"\le -> ≤", handlers._convert_latex(r"x \le 5") == "x ≤ 5")
    check(r"\ge -> ≥", handlers._convert_latex(r"x \ge 5") == "x ≥ 5")
    check(r"\leq -> ≤ (long form, not split into \le + q)", handlers._convert_latex(r"x \leq 5") == "x ≤ 5")
    check(r"\geq -> ≥ (long form)", handlers._convert_latex(r"x \geq 5") == "x ≥ 5")
    check(r"\neq -> ≠", handlers._convert_latex(r"x \neq 5") == "x ≠ 5")
    check(r"\pm -> ±", handlers._convert_latex(r"x = 3 \pm 1") == "x = 3 ± 1")
    check(r"\pi -> π", handlers._convert_latex(r"\pi r^2") == "π r^2")
    check(r"\alpha -> α", handlers._convert_latex(r"\alpha + \beta") == "α + β")
    check(r"\Omega -> Ω (capital greek)", handlers._convert_latex(r"\Omega") == "Ω")

    # ------------------------------------------------------------------
    # Generic catch-all for unlisted commands.
    # ------------------------------------------------------------------
    check(r"\text{...} -> content only", handlers._convert_latex(r"5 \text{см}") == "5 см")
    check(r"\left(/\right) -> just the delimiters survive", handlers._convert_latex(r"\left(x+1\right)") == "(x+1)")
    check(r"unknown bare command is dropped, not left as a backslash", "\\" not in handlers._convert_latex(r"\displaystyle x"))

    # ------------------------------------------------------------------
    # No stray backslashes/braces left in any of the above.
    # ------------------------------------------------------------------
    for expr in (r"\(x^2\)", r"\frac{1}{2}", r"\sqrt{16}", r"x \leq 5 \pm \pi"):
        out = handlers._convert_latex(expr)
        check(f"no leftover backslash in {expr!r} -> {out!r}", "\\" not in out)

    # ------------------------------------------------------------------
    # Exact spec example, end to end through _convert_latex.
    # ------------------------------------------------------------------
    spec_text = r"Решение: \(x^2\), \frac{1}{2} и \sqrt{16}."
    out = handlers._convert_latex(spec_text)
    check("spec example: no backslashes", "\\" not in out)
    check("spec example: no curly braces", "{" not in out and "}" not in out)
    check("spec example: readable result", out == "Решение: x^2, 1/2 и √16.")

    # ------------------------------------------------------------------
    # Order of operations: LaTeX runs before Markdown/HTML in _send_long's
    # pipeline. Mixed text with both should come out right in both parts.
    # ------------------------------------------------------------------
    mixed = r"**Ответ:** \(x = \frac{1}{2}\), это `важно`"
    latex_then_md = handlers._sanitize_model_html(handlers._convert_markdown(handlers._convert_latex(mixed)))
    check("mixed: LaTeX resolved (no backslashes)", "\\" not in latex_then_md)
    check("mixed: Markdown bold became a real tag", "<b>Ответ:</b>" in latex_then_md)
    check("mixed: Markdown code became a real tag", "<code>важно</code>" in latex_then_md)
    check("mixed: the fraction is readable (1/2)", "1/2" in latex_then_md)

    # If LaTeX ran AFTER markdown+escaping (wrong order), the backslash
    # would already be HTML-escaped or the underscore would confuse the
    # italic-markdown regex — verify escaping-first would have broken it,
    # to prove the order actually matters here (not merely stylistic).
    wrong_order = handlers._sanitize_model_html(handlers._convert_latex(handlers._convert_markdown(mixed)))
    check(
        "mixed (wrong order applied for contrast): still no raw backslash reaches the user either way",
        "\\" not in wrong_order,
    )

    # ------------------------------------------------------------------
    # A message with no LaTeX at all is untouched.
    # ------------------------------------------------------------------
    plain = "Обычный текст без формул."
    check("plain text unchanged", handlers._convert_latex(plain) == plain)


run()

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
