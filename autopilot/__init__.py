"""autopilot — orquestrador autônomo para o ciclo de implementação do BMAD v6.

Roda as skills do BMAD (create-story -> dev-story -> code-review, e
retrospective ao fim da epic) em sessões worker separadas, e roteia cada
decisão que uma skill levantaria para uma sessão "advisor" que escolhe a
melhor opção para o sistema.
"""

__version__ = "0.1.0"
