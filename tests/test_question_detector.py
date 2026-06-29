"""Testes do detector de pergunta-em-texto (rede de segurança do worker).

Cobre os casos que DEVEM acionar o advisor (pergunta/decisão em texto livre) e
os que NÃO devem (conclusão/relatório), incluindo os formatos reais observados:
um checkpoint "(Y to continue)" e um HALT com menu numerado em português.
"""

from autopilot.worker import _looks_like_question

# ---- DEVEM disparar (worker pausou pedindo decisão) --------------------

SHOULD_DETECT = {
    "ask_tag": "<ask>Choose option [1] or [q] to quit:</ask>",
    "choose_option": "Choose option [1], [2], or [3]:",
    "yes_no": "Continue with the incomplete epic? (yes/no)",
    "y_slash_n": "Apply the patch now? (y/n)",
    # o caso real do code-review: termina em ")", não em "?"
    "y_to_continue": (
        "Here's the Step 1 checkpoint before I launch the review.\n\n"
        "Shall I proceed to Step 2 — launch the parallel adversarial reviews? "
        "(Y to continue)"
    ),
    "proceed_q": "Proceed?",
    "shall_i": "Shall I commit these changes and open the PR now?",
    # "?" não está na última linha, mas está nas últimas linhas
    "question_not_last_line": (
        "Which approach do you prefer?\n"
        "Let me know and I'll continue."
    ),
    # português: menu + HALT (o "?" está em 'Próximo passo?' e há HALT/aguardando)
    "halt_menu_pt": (
        "🎉 Code review da Story 3.3 — COMPLETO\n\n"
        "Veredito: implementação real; as 4 ACs provadas.\n\n"
        "▎ Próximo passo?\n"
        "▎ 1. Commitar os patches\n"
        "▎ 2. Encerrar — você commita/abre o PR\n"
        "▎ 3. Próxima story (dev-story)\n\n"
        "HALT — aguardando sua escolha."
    ),
    "qual_opcao_pt": (
        "Encontrei duas formas de implementar.\n"
        "Qual opção você prefere?\n"
        "1. Backend + fetch\n"
        "2. Só o modelo de dados"
    ),
    "press_enter": "Setup complete. Press ENTER to continue.",
}

# ---- NÃO devem disparar (worker concluiu / só relatou) -----------------

SHOULD_NOT_DETECT = {
    "empty": "",
    "blank": "   \n  \n",
    "completion": "All tests green. Ready for review.",
    "status_done": "Implementation complete. sprint-status updated to done.",
    "prose": "Refactored the util and removed the dead findSummaryById method.",
    # changelog numerado de fim de review SEM pergunta -> não é menu de escolha
    "numbered_changelog": (
        "Patches aplicados (todos verdes):\n"
        "1. category-prisma-error.util agora discrimina pelo constraint\n"
        "2. Removido findSummaryById morto\n"
        "3. Teste de paridade prova o .strict()\n"
        "Gate de verificação: 148 ✓ + typecheck/lint ✓"
    ),
    # bullets não são menu numerado
    "bullet_summary": "Resumo:\n- patch 1 aplicado\n- patch 2 aplicado\n- nada bloqueante",
}


def test_should_detect():
    for name, text in SHOULD_DETECT.items():
        assert _looks_like_question(text), f"deveria detectar pergunta em: {name}"


def test_should_not_detect():
    for name, text in SHOULD_NOT_DETECT.items():
        assert not _looks_like_question(text), f"NÃO deveria detectar em: {name}"
