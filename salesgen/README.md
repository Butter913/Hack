# salesgen: generator rozmów sprzedażowych z zaszytymi powodami odmowy

Generuje rozmowy telesprzedażowe przez ElevenLabs Agents i zapisuje je w formacie challenge'u (`histories.jsonl`,
`questions.jsonl`, `gold.jsonl`), więc `reactor/client.py`, `reactor/predictions.py` i `score.py` działają bez zmian.

Każda rozmowa jest **zaplanowana przed symulacją**: wiadomo z góry, czy klient kupi, jaki jest jego prawdziwy
powód odmowy i według jakiego wzorca (near-missa) przebiega rozmowa. To jest gold, na którym mierzycie pipeline.

## Jak to działa

```
plan       persony z zaszytym wynikiem i powodem          → runs/X/plan.jsonl
simulate   ElevenLabs: agent-sprzedawca ↔ symulowany klient → runs/X/transcripts.jsonl
build      format challenge'u + pytania + gold              → runs/X/dataset/
audio      (opcjonalnie) nagrania MP3 przez text-to-speech  → runs/X/audio/
```

- **Sprzedawca** to jeden agent ElevenLabs (tworzony raz, id w `runs/X/state.json`). Produkt, cena i imię klienta
  trafiają do niego przez dynamic variables, inne w każdej rozmowie.
- **Klient** to symulowany użytkownik w teście typu `simulation`. Jego persona (`simulation_scenario`) zawiera
  ukryty powód odmowy, temperament i wzorzec rozmowy. Podgląd pokazuje `plan`.
- **Kontrola jakości:** każdy test ma `success_conditions`, np. „klient nie kupuje” i „klient podaje powód X”.
  ElevenLabs ocenia transkrypt. Rozmowy, które nie trzymają się planu (`failed`), `build` domyślnie odrzuca
  (`--keep-failed`, żeby je zostawić).
- Symulacja jest **tekstowa** (LLM vs LLM). Głos to osobny krok `audio`, który czyta gotowy transkrypt dwoma głosami.
- Używane jest nowe API testów agentów (`/v1/convai/agent-testing/create`, `/v1/convai/agents/{id}/run-tests`).
  Stary endpoint `simulate-conversation` jest deprecated i znika 31.10.2026.

## Krok po kroku

1. **Klucz API.** W ElevenLabs: Settings → API Keys. Klucz potrzebuje dostępu do ElevenAgents (tworzenie agentów
   i testów) oraz Text to Speech, jeśli chcecie audio. Wpiszcie go przy ukrytym prompcie, a nie w komendzie:

   ```bash
   printf 'ElevenLabs key: '; read -rs ELEVENLABS_API_KEY; echo; export ELEVENLABS_API_KEY
   ```

2. **Plan.** Obejrzyjcie rozkład wzorców i przykładową personę:

   ```bash
   python salesgen/generate.py plan --run salesgen/runs/demo --calls 200 --lang pl
   ```

3. **Próba na 5 rozmowach.** Sprawdźcie jakość i koszt (`credits` w `transcripts.jsonl`):

   ```bash
   python salesgen/generate.py simulate --run salesgen/runs/demo --limit 5
   ```

4. **Reszta.** Polecenie jest wznawialne: przerwane albo ponowione pomija rozmowy już zapisane.

   ```bash
   python salesgen/generate.py simulate --run salesgen/runs/demo
   ```

5. **Dataset:**

   ```bash
   python salesgen/generate.py build --run salesgen/runs/demo
   ```

6. **Audio na demo (opcjonalnie).** ID głosów skopiujecie z Voice Library w ElevenLabs:

   ```bash
   python salesgen/generate.py audio --run salesgen/runs/demo --seller-voice <voice_id> --customer-voice <voice_id> --limit 10
   ```

Bez klucza całość da się przetestować offline: `simulate --mock` pisze krótkie skryptowe transkrypty zgodne
z planem. Tekst jest nierealistyczny, ale `build` i scorer działają.

Przydatne flagi `simulate`: `--batch` (testy na jedno uruchomienie, domyślnie 10), `--max-turns` (16),
`--agent-llm` (LLM sprzedawcy), `--user-llm` (LLM klienta), `--agent-id` (użycie istniejącego agenta),
`--keep-tests` (nie kasuj testów z workspace'u po pobraniu wyników).

## Co jest zaszyte

**Powody odmowy** (`scenarios.REASONS`) z planowanym udziałem wśród odmów: cena 32%, zły moment 20%,
konkurencja 16%, brak zaufania 14%, brak funkcji 10%, brak potrzeby 8%. Każdy powód ma kilka konkretnych wariantów,
np. „przeprowadzka za dwa miesiące” czy „szwagier pracuje u konkurencji”. Ta sama kategoria brzmi więc za każdym
razem inaczej i to właśnie klastrowanie ma potem złożyć z powrotem w jedną grupę. Proporcje trzymają się dokładnie
dzięki losowaniu z talii, a nie niezależnemu losowaniu każdej rozmowy.

**Błędy sprzedawcy** (`scenarios.SELLER_MISTAKES`). Ok. 30% rozmów to sprzedaże zawalone przez sprzedawcę:
klient był otwarty, ale sprzedawca popełnił błąd. Błąd trafia do agenta jako zmienna `{{seller_style}}` osobno
w każdej rozmowie, a persona klienta mówi, jak na niego zareagować:

| Błąd | Udział | Co robi sprzedawca |
|---|---|---|
| `no_close` | 32% | nie prosi o decyzję ani nie proponuje kolejnego kroku |
| `objection_dismissed` | 24% | zbywa konkretne pytanie klienta ogólnikiem |
| `pushy` | 18% | naciska mimo wahania klienta |
| `no_discovery` | 14% | nie pyta o potrzeby i forsuje niepasującą ofertę |
| `wrong_info` | 12% | podaje sprzeczną cenę lub warunki umowy |

Łącznie ok. 80% rozmów kończy się porażką: z winy klienta albo sprzedawcy.

**Wzorce rozmów** (`scenarios.PATTERNS`), czyli near-missy:

| Wzorzec | Wynik | Pułapka dla pipeline'u |
|---|---|---|
| `plain_no_sale` | odmowa | brak, przypadek bazowy |
| `plain_sale` | zakup | brak |
| `price_grumble_sale` | zakup | narzeka na cenę, ale kupuje |
| `polite_brushoff` | odmowa | prawdziwy powód pada raz w środku, na końcu tylko „przemyślę to” |
| `agent_wrong_guess` | odmowa | sprzedawca zgaduje „cena?”, klient zaprzecza i podaje inny powód |
| `two_reasons` | odmowa | dwa powody, liczy się główny |
| `callback` | follow-up | „proszę zadzwonić za miesiąc”, ani zakup, ani odmowa |
| `seller_fault` | odmowa | klient chciał kupić, sprzedaż zawalił sprzedawca (błędy powyżej) |

**Klienci z kilkoma rozmowami:** ok. 25% klientów, którzy nie kupili, dostaje drugą rozmowę 2–4 tygodnie później.
W połowie takich przypadków powód się zmienia, np. najpierw „zły moment”, potem „już podpisałem u konkurencji”.
To odpowiednik knowledge-update z benchmarku.

## Co wychodzi z `build`

`runs/X/dataset/`:

- `histories.jsonl`: jedna historia = cała kampania, sesja = jedna rozmowa. Klient ma rolę `user`, sprzedawca
  `assistant`. W `meta` jest `call_id`, `customer_id`, `customer_name` i `product_id`. **Wyniku rozmowy i powodu
  w historii nie ma**, bo to pipeline ma je znaleźć.
- `questions.jsonl` i `gold.jsonl`: pytania z odpowiedziami i evidence (`session_id`):

  | `question_type` | Przykład |
  |---|---|
  | `aggregate` | Jaki jest najczęstszy powód, dla którego klienci nie kupili…? |
  | `aggregate-count` | Ilu klientów odmówiło zakupu głównie z powodu: zły moment? |
  | `aggregate-seller` | Jaki błąd sprzedawcy najczęściej powodował, że rozmowa nie kończyła się sprzedażą? |
  | `aggregate-fault` | W ilu nieudanych rozmowach winny był sprzedawca, a w ilu klient? |
  | `seller-fault/<błąd>` | Dlaczego … nie kupił…? (odpowiedź: błąd sprzedawcy) |
  | `aggregate-product` | Jaki jest najczęstszy powód odmowy zakupu oferty „Internet światłowodowy 1 Gb/s”? |
  | `customer-reason/<wzorzec>` | Dlaczego Ewa Mazur nie kupiła oferty „Instalacja fotowoltaiczna 6 kW”? |
  | `knowledge-update` | Jaki jest najbardziej aktualny powód, dla którego … nie kupił oferty …? |
  | `abstain/bought` | Pytanie „dlaczego nie kupił” o klienta, który kupił (najpierw ci, co narzekali na cenę) |
  | `abstain/unknown` | Pytanie o klienta, do którego nikt nie dzwonił |

  Agregaty liczą **klientów** według ich ostatniej rozmowy, a nie rozmowy.
- `calls_gold.jsonl`: etykieta każdej rozmowy (wynik, wzorzec, czyja wina: `fault` = `customer`/`seller`,
  powód główny i poboczny, szczegół). Służy do
  mierzenia samej ekstrakcji i klastrowania, np. czystości klastrów względem `primary_reason`.

Ocena: `python score.py salesgen/runs/demo/dataset/gold.jsonl predictions.jsonl` daje wynik osobno dla każdego
typu pytania.
