import os
import json
import base64
import mimetypes
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY не найден")


# ============================================================
# OPENROUTER
# ONLY FOR HOMEWORK CHECKING
# ============================================================

homework_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    timeout=180.0,
    max_retries=0,
)

HOMEWORK_MODEL = "qwen/qwen3-vl-32b-instruct"


# ============================================================
# GROQ
# ONLY FOR ORDINARY CHAT
#
# Groq is initialized lazily.
# Homework checking does not depend on Groq.
# ============================================================

chat_client = None
CHAT_MODEL = "openai/gpt-5-mini"


def _get_chat_client():
    global chat_client

    if chat_client is None:
        groq_api_key = os.environ.get("GROQ_API_KEY")

        if not groq_api_key:
            raise RuntimeError("GROQ_API_KEY не найден")

        chat_client = OpenAI(
            api_key=groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            timeout=120.0,
            max_retries=0,
        )

    return chat_client


# ============================================================
# RESULT MODELS
# ============================================================

class ParentApprovalRequest(BaseModel):
    photo_index: int = Field(ge=0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    question: str
    ai_interpretation: str
    alternatives: list[str] = []


class HomeworkResult(BaseModel):
    is_homework: bool
    correct: bool
    confidence: float = Field(ge=0.0, le=1.0)
    correctness_percent: int = Field(ge=0, le=100)

    subject: str
    content_type: str

    summary: str
    mistakes: list[str]
    recommendation: str

    needs_parent_approval: bool = False
    parent_approval_requests: list[ParentApprovalRequest] = []


class DiaryHomework(BaseModel):
    subject: str
    task: str = ""
    homework_present: bool = True


class DiaryResult(BaseModel):
    has_homework: bool
    items: list[DiaryHomework] = []
    summary: str = ""
    today_found: bool = True
    issues: list[str] = []
    needs_parent_approval: bool = False


class TextSubmissionResult(BaseModel):
    accepted: bool
    needs_photo: bool
    needs_voice: bool = False
    percent: int = Field(ge=0, le=100)
    summary: str = ""


# ============================================================
# COMMON SYSTEM INSTRUCTIONS
# ============================================================

BASE_RULES = """
Ты проверяешь школьную домашнюю работу по фотографиям.

КРИТИЧЕСКИ ВАЖНО:

1. Проверяй только то, что реально видно на фотографии.
2. Никогда не угадывай неразборчивый почерк.
3. Сначала установи, что именно написал ученик.
4. Только после этого проверяй правильность.
5. Все фотографии являются одной домашней работой.
6. Проверяй все задания на всех фотографиях.
7. Не пропускай маленькие, короткие или частично видимые ответы.
8. Если написанное невозможно уверенно прочитать — не придумывай его.
9. Если ответ зачёркнут, проверяй финальный незачёркнутый вариант.
10. Исправление само по себе не является ошибкой, если итоговый вариант правильный.
11. Проверяй именно написание ученика, а не предполагаемый правильный вариант.

ОСОБОЕ ВНИМАНИЕ К ПОЧЕРКУ:

Отдельно перепроверяй похожие буквы:

Е / И
Е / Ё
О / А
И / Ы
А / Я
Ь / Ъ
С / З
Б / П
В / Ф
Г / К
Д / Т
Ж / Ш
Ч / Щ
Ц / С

Если буква выглядит неоднозначно:
- сравни её с другими буквами этого ученика;
- используй контекст только как дополнительный фактор;
- не заменяй реально написанную букву предполагаемой.

Если слово можно прочитать несколькими способами,
считай его сомнительным и передай его на дополнительную проверку.

НЕРАЗБОРЧИВЫЙ ОТВЕТ:

Если невозможно уверенно определить существенную часть ответа,
нельзя считать работу полностью правильной.

100% возможно только тогда, когда:
- все задания найдены;
- все задания выполнены;
- все ответы читаемы;
- все ответы проверены;
- нет ни одной ошибки;
- все требования задания выполнены.

РЕАЛЬНЫЕ ОШИБКИ:

Для каждой ошибки используй:

"Задание N: написано «X», должно быть «Y». Тип: Z. Причина: ..."

Не придумывай ошибки.

Если работа правильная — mistakes должен быть пустым.

Не исправляй почерк ученика мысленно.
Сначала установи фактический текст, потом проверяй его.
"""


# ============================================================
# IMAGE → DATA URL
# ============================================================

def image_to_data_url(image_path: str) -> str:
    path = Path(image_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Файл изображения не найден: {image_path}"
        )

    mime_type, _ = mimetypes.guess_type(path.name)

    if not mime_type:
        mime_type = "image/jpeg"

    with path.open("rb") as file:
        encoded = base64.b64encode(file.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"


# ============================================================
# GENERIC QWEN REQUEST
# ============================================================

def _request_qwen(content: list, stage_name: str) -> str:
    """
    Любой запрос проверки домашки идёт ТОЛЬКО:
        homework_client → OpenRouter → Qwen

    Groq здесь никогда не используется.
    """

    try:
        response = homework_client.chat.completions.create(
            model=HOMEWORK_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            temperature=0,
            max_completion_tokens=2500,
            response_format={
                "type": "json_object"
            },
        )

    except Exception as exc:
        raise RuntimeError(
            f"Ошибка Qwen/OpenRouter на этапе {stage_name}: {exc}"
        ) from exc

    if not response.choices:
        raise RuntimeError(
            f"Qwen не вернул choices на этапе {stage_name}"
        )

    message = response.choices[0].message

    if not message or not message.content:
        raise RuntimeError(
            f"Qwen вернул пустой ответ на этапе {stage_name}"
        )

    return message.content.strip()


def chat_with_ai(
    user_message: str,
    image_paths: list[str] | None = None,
    history: list[dict] | None = None,
) -> str:
    """OpenRouter GPT-5 Mini chat with optional image and short conversation history."""
    content = [{"type": "text", "text": user_message}]
    for image_path in image_paths or []:
        content.append({
            "type": "image_url",
            "image_url": {"url": image_to_data_url(image_path)},
        })

    system_prompt = """
You are a helpful educational AI assistant and chat partner.
Reply in Russian unless the user asks for another language.
Keep answers concise and structured, but never superficial.
Use short headings, numbered steps, lists, formulas, simple schemes, and cause-and-effect logic when useful.
Explain the reasoning and the solution path instead of only giving a final answer.
Remove repetition and irrelevant digressions. State assumptions or uncertainty clearly.
If the request is ambiguous, ask one precise clarifying question.
"""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend((history or [])[-12:])
    messages.append({"role": "user", "content": content})

    try:
        response = homework_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=1200,
        )
    except Exception as exc:
        raise RuntimeError(f"Ошибка OpenRouter/GPT-5 Mini: {exc}") from exc

    if not response.choices or not response.choices[0].message.content:
        raise RuntimeError("OpenRouter вернул пустой ответ")
    return response.choices[0].message.content.strip()


def _parse_json(raw_result: str, stage_name: str) -> dict:
    try:
        return json.loads(raw_result)

    except json.JSONDecodeError as exc:
        print(
            f"[AI ERROR] Невалидный JSON на этапе {stage_name}: "
            f"{exc}"
        )
        print(raw_result)

        raise RuntimeError(
            f"Qwen вернул невалидный JSON на этапе {stage_name}"
        ) from exc


def assess_text_submission(task: str, response: str) -> TextSubmissionResult:
    prompt = f"""
Оцени текстовый ответ ребёнка на задание из дневника.

Задание: {task}
Ответ ребёнка: {response}

Правила:
- Организационные задания вроде «взять тетрадь», «собрать гербарий»,
  «подготовить форму», «принести материалы» можно принять по короткому
  подтверждению ребёнка.
- Для «прочитать», «выучить», «пересказать», «сделать упражнение»,
  «решить», «написать», «нарисовать» одного сообщения «сделал» недостаточно.
  Нужна фотография результата; для чтения дополнительно нужен голосовой
  пересказ. В этих случаях needs_photo=true или needs_voice=true.
- Не принимай заявление о выполнении за доказательство письменного результата.
- Ответ строго JSON без пояснений:
{{"accepted": true/false, "needs_photo": true/false, "needs_voice": true/false, "percent": 0-100, "summary": "коротко"}}
"""
    raw = _request_qwen([{"type": "text", "text": prompt}], "TEXT SUBMISSION")
    data = _parse_json(raw, "TEXT SUBMISSION")
    data["summary"] = " ".join(str(data.get("summary") or "").split())[:240]
    return TextSubmissionResult.model_validate(data)


def transcribe_voice(audio_path: str) -> str:
    """Transcribe a Telegram voice message through Groq Whisper."""
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(audio_path)

    client = _get_chat_client()
    try:
        with path.open("rb") as audio_file:
            result = client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=audio_file,
                response_format="text",
                language="ru",
            )
    except Exception as exc:
        raise RuntimeError(f"Ошибка транскрибации голосового сообщения: {exc}") from exc

    text = getattr(result, "text", None) or str(result)
    text = " ".join(text.split()).strip()
    if not text:
        raise RuntimeError("Сервис транскрибации вернул пустой текст")
    return text


def assess_reading_submission(task: str, transcript: str, photo_paths: list[str]) -> TextSubmissionResult:
    """Assess a child's short spoken retelling against a reading assignment."""
    prompt = f"""
Оцени устный краткий пересказ ребёнка по заданию из дневника.

Задание: {task}
Расшифровка голосового сообщения: {transcript}

Правила:
- Сравни содержание пересказа с тем, что требовалось прочитать.
- Не требуй дословного пересказа: учитывай смысл, ключевые события и понимание текста.
- Если пересказ пустой, случайный, не относится к заданию или ребёнок только говорит «сделал» — не принимай.
- Фото текста уже запрошено отдельно; по этой функции оценивай именно наличие осмысленного пересказа.
- accepted=true только если пересказ достаточно подтверждает чтение и понимание.
- percent — оценка качества пересказа от 0 до 100; успешная сдача выше 75.
- Ответь строго JSON без пояснений:
{{"accepted": true/false, "needs_photo": false, "needs_voice": false, "percent": 0-100, "summary": "краткий вывод до 240 символов"}}
"""
    content = _build_image_content(photo_paths, prompt)
    raw = _request_qwen(content, "READING SUBMISSION")
    data = _parse_json(raw, "READING SUBMISSION")
    data["needs_photo"] = False
    data["needs_voice"] = False
    data["summary"] = " ".join(str(data.get("summary") or "").split())[:240]
    return TextSubmissionResult.model_validate(data)


# ============================================================
# BUILD IMAGE CONTENT
# ============================================================

def _build_image_content(
    photo_paths: list[str],
    prompt: str,
) -> list:

    content = [
        {
            "type": "text",
            "text": prompt,
        }
    ]

    for photo_path in photo_paths:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": image_to_data_url(photo_path),
                },
            }
        )

    return content


# ============================================================
# PASS 1
# OCR / RECOGNITION
# ============================================================

def _pass_one_recognition(
    photo_paths: list[str],
    subject: str,
) -> dict:

    prompt = f"""
{BASE_RULES}

ЭТО ПЕРВЫЙ ПРОХОД.

Твоя задача сейчас — максимально точно РАСПОЗНАТЬ работу.

Предмет: {subject}

Не пытайся просто угадать правильные ответы.

Для каждого задания установи:
- номер задания;
- что написал ученик;
- насколько уверенно прочитан ответ;
- есть ли отдельные слова или буквы, которые невозможно
  уверенно распознать.

Особенно внимательно ищи потенциальные различия:
О/А, И/Ы, Е/И, Ь/Ы, Е/Ё и другие похожие буквы.

Если сомневаешься — НЕ угадывай.
Отметь это как ambiguous.

Для каждого сомнительного места обязательно укажи:
- номер фотографии;
- нормализованные координаты области на фотографии:
  x, y, width, height — от 0.0 до 1.0;
- вопрос для человека, который будет смотреть фрагмент.

JSON:

{{
  "is_homework": true,
  "recognition_confidence": 0.0,
  "transcription": [
    {{
      "task": "1",
      "student_text": "точно распознанный текст",
      "confidence": 0.95,
      "ambiguous": false
    }}
  ],
  "ambiguous_places": [
    {{
      "photo_index": 0,
      "x": 0.10,
      "y": 0.20,
      "width": 0.30,
      "height": 0.10,
      "question": "Что написано в этом месте?",
      "ai_interpretation": "реинкарнация",
      "alternatives": ["реинкарнация", "реинкорнация"]
    }}
  ]
}}

Если сомнительных мест нет:
"ambiguous_places": []

Верни только JSON.
"""

    print("[AI PASS 1] Распознавание почерка")

    raw = _request_qwen(
        _build_image_content(photo_paths, prompt),
        "PASS 1",
    )

    result = _parse_json(raw, "PASS 1")

    print("===== PASS 1 RESULT =====")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=========================")

    return result


# ============================================================
# PASS 2
# CORRECTNESS CHECK
# ============================================================

def _pass_two_check(
    photo_paths: list[str],
    subject: str,
    recognition: dict,
) -> dict:

    recognition_json = json.dumps(
        recognition,
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""
{BASE_RULES}

ЭТО ВТОРОЙ ПРОХОД.

Сейчас ты должен независимо проверить правильность работы.

Предмет: {subject}

Первый проход дал следующее распознавание:

{recognition_json}

ВАЖНО:

Не принимай распознавание первого прохода автоматически за истину.

Снова посмотри на фотографии.

Для каждого ответа:
1. проверь, действительно ли именно это написал ученик;
2. сравни с правильным вариантом;
3. проверь орфографию;
4. проверь окончания и формы слов;
5. проверь остальные требования задания.

Особенно ищи скрытые ошибки:
- О вместо А;
- А вместо О;
- И вместо Ы;
- Ы вместо И;
- Е вместо И;
- Ь вместо Ы и наоборот;
- Е/Ё;
- похожие согласные.

Если первый проход что-то прочитал неправильно,
исправь его.

Если нашёл ошибку — зафиксируй её.

Если не уверен, что именно написано,
НЕ ПРИДУМЫВАЙ. Создай ambiguous_place с координатами.

JSON:

{{
  "is_homework": true,
  "confidence": 0.0,
  "correctness_percent": 0,
  "mistakes": [
    "Задание 1: написано «X», должно быть «Y». Тип: ... Причина: ..."
  ],
  "ambiguous_places": [
    {{
      "photo_index": 0,
      "x": 0.10,
      "y": 0.20,
      "width": 0.30,
      "height": 0.10,
      "question": "Что написано в этом месте?",
      "ai_interpretation": "вариант",
      "alternatives": ["вариант 1", "вариант 2"]
    }}
  ]
}}

Если ошибок нет:
"mistakes": []

Если сомнительных мест нет:
"ambiguous_places": []

Верни только JSON.
"""

    print("[AI PASS 2] Проверка правильности")

    raw = _request_qwen(
        _build_image_content(photo_paths, prompt),
        "PASS 2",
    )

    result = _parse_json(raw, "PASS 2")

    print("===== PASS 2 RESULT =====")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=========================")

    return result


# ============================================================
# PASS 3
# FINAL AUDIT
# ============================================================

def _pass_three_audit(
    photo_paths: list[str],
    subject: str,
    recognition: dict,
    correctness: dict,
) -> dict:

    recognition_json = json.dumps(
        recognition,
        ensure_ascii=False,
        indent=2,
    )

    correctness_json = json.dumps(
        correctness,
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""
{BASE_RULES}

ЭТО ТРЕТИЙ И ФИНАЛЬНЫЙ ПРОХОД.

Ты являешься независимым аудитором.

Предмет: {subject}

Результат первого прохода:

{recognition_json}

Результат второго прохода:

{correctness_json}

Теперь снова внимательно изучи ВСЕ фотографии.

Твоя задача — специально найти ошибки, которые могли пропустить
первые два прохода.

Не доверяй предыдущим результатам автоматически.

Проверяй особенно:

1. Орфографические ошибки.
2. О/А.
3. И/Ы.
4. Е/И.
5. Е/Ё.
6. Ь/Ъ.
7. Ж/Ш.
8. Ч/Щ.
9. Ц/С.
10. З/С.
11. Б/П.
12. В/Ф.
13. Г/К.
14. Д/Т.
15. Неправильные окончания.
16. Неправильные падежи.
17. Пропущенные буквы.
18. Лишние буквы.
19. Незаконченные задания.
20. Ошибки, которые появились из-за неверного чтения почерка.

Для каждого найденного места снова установи:
ЧТО НАПИСАНО → ЧТО ДОЛЖНО БЫТЬ.

Если не можешь уверенно установить написанное —
не угадывай и передай место человеку.

ВАЖНО:

Если предыдущие проходы ошибочно посчитали работу правильной,
ты обязан это исправить.

JSON:

{{
  "is_homework": true,
  "confidence": 0.0,
  "correctness_percent": 0,
  "mistakes": [
    "Задание N: написано «X», должно быть «Y». Тип: ... Причина: ..."
  ],
  "ambiguous_places": [
    {{
      "photo_index": 0,
      "x": 0.10,
      "y": 0.20,
      "width": 0.30,
      "height": 0.10,
      "question": "Что написано в этом месте?",
      "ai_interpretation": "вариант",
      "alternatives": ["вариант 1", "вариант 2"]
    }}
  ]
}}

Верни только JSON.
"""

    print("[AI PASS 3] Финальный аудит")

    raw = _request_qwen(
        _build_image_content(photo_paths, prompt),
        "PASS 3",
    )

    result = _parse_json(raw, "PASS 3")

    print("===== PASS 3 RESULT =====")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=========================")

    return result


# ============================================================
# MERGE RESULTS
# ============================================================

def _merge_parent_requests(
    recognition: dict,
    correctness: dict,
    audit: dict,
) -> list[ParentApprovalRequest]:

    all_places = []

    for source in (
        recognition,
        correctness,
        audit,
    ):
        places = source.get("ambiguous_places", [])

        if isinstance(places, list):
            all_places.extend(places)

    unique = []
    seen = set()

    for place in all_places:
        try:
            photo_index = int(place.get("photo_index", 0))

            x = float(place.get("x", 0))
            y = float(place.get("y", 0))
            width = float(place.get("width", 0))
            height = float(place.get("height", 0))

            question = str(
                place.get(
                    "question",
                    "Что написано на этом фрагменте?",
                )
            )

            interpretation = str(
                place.get(
                    "ai_interpretation",
                    "",
                )
            )

            alternatives = place.get(
                "alternatives",
                [],
            )

            if not isinstance(alternatives, list):
                alternatives = []

            alternatives = [
                str(value)
                for value in alternatives
            ]

            # Normalize values for duplicate detection.
            key = (
                photo_index,
                round(x, 2),
                round(y, 2),
                round(width, 2),
                round(height, 2),
            )

            if key in seen:
                continue

            seen.add(key)

            # Basic validation.
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            width = max(0.01, min(1.0 - x, width))
            height = max(0.01, min(1.0 - y, height))

            unique.append(
                ParentApprovalRequest(
                    photo_index=photo_index,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    question=question,
                    ai_interpretation=interpretation,
                    alternatives=alternatives,
                )
            )

        except Exception as exc:
            print(
                f"[AI WARNING] Не удалось обработать "
                f"сомнительное место: {exc}"
            )

    return unique


# ============================================================
# HOMEWORK CHECK
# ============================================================

def check_diary(photo_paths: list[str], day_name: str, expected_subjects: list[str] | None = None, today_date: str | None = None) -> DiaryResult:
    if not photo_paths:
        raise ValueError("Фотографии дневника отсутствуют")

    expected_text = ", ".join(expected_subjects or []) or "не задан; определи все предметы, видимые в строке сегодняшнего дня"
    date_text = today_date or "дата не передана"
    base_prompt = f"""
Ты проверяешь фотографию школьного дневника ученика.
Сегодня: {day_name}.
Сегодняшняя дата: {date_text}.

Ожидаемые предметы сегодняшнего расписания: {expected_text}

Проверь только строку/колонку сегодняшнего дня. Для КАЖДОГО ожидаемого
предмета создай запись в items: homework_present=true и точный текст задания,
либо homework_present=false и task="", если поле явно пустое.
Если предмет сегодняшнего дня не найден, перепутан с другим днём, закрыт,
замазан, зачёркнут, обрезан, размыт, неразборчив или невозможно понять,
что там написано, обязательно добавь это в issues. Любая такая мелочь означает
needs_parent_approval=true. Не считай поле пустым, если его содержимое скрыто.
Обязательно проверь вертикальную дату строки и месяц в верхней части дневника.
Если дата или месяц не совпадают с сегодняшней датой, либо дату нельзя уверенно
прочитать, добавь это в issues. Нельзя угадывать текст, предмет или день.
issues пиши кратко: одна причина в одном коротком предложении, без повторов,
не более трёх замечаний. summary — не более двух коротких предложений.
Ответ строго JSON:
{{"today_found": true/false, "has_homework": true/false, "items": [{{"subject": "...", "task": "...", "homework_present": true/false}}], "issues": ["..."], "summary": "..."}}
"""
    pass1 = _parse_json(_request_qwen(_build_image_content(photo_paths, base_prompt + "\nЭто этап 1: полное распознавание."), "DIARY PASS 1"), "DIARY PASS 1")
    pass2_prompt = base_prompt + f"""
Это этап 2: независимый построчный аудит. Найди пропуски и ошибки в первом
распознавании. Первый результат для сверки:
{json.dumps(pass1, ensure_ascii=False)}
"""
    pass2 = _parse_json(_request_qwen(_build_image_content(photo_paths, pass2_prompt), "DIARY PASS 2"), "DIARY PASS 2")
    pass3_prompt = base_prompt + f"""
Это этап 3: финальный строгий аудит. Сверь оба результата, перечисли любую
неуверенность и не скрывай ни одного спорного поля.
Этап 1:
{json.dumps(pass1, ensure_ascii=False)}
Этап 2:
{json.dumps(pass2, ensure_ascii=False)}
"""
    data = _parse_json(_request_qwen(_build_image_content(photo_paths, pass3_prompt), "DIARY PASS 3"), "DIARY PASS 3")
    if not isinstance(data.get("items", []), list):
        data["items"] = []
    issues = []
    for source in (pass1, pass2, data):
        for issue in source.get("issues", []) if isinstance(source.get("issues", []), list) else []:
            issue = str(issue).strip()
            if issue and issue not in issues:
                issues.append(issue)
        if not source.get("today_found", True):
            issue = "Один из этапов не смог уверенно определить сегодняшний день."
            if issue not in issues:
                issues.append(issue)
        for item in source.get("items", []) if isinstance(source.get("items", []), list) else []:
            if item.get("homework_present", False) and not str(item.get("task", "")).strip():
                issue = f"У предмета «{item.get('subject', 'неизвестно')}» отмечено ДЗ без читаемого текста."
                if issue not in issues:
                    issues.append(issue)
    if not data.get("today_found", True):
        issues.append("Не удалось уверенно найти сегодняшний день.")
    compact_issues = []
    for issue in issues:
        issue = " ".join(str(issue).split())
        if issue and issue not in compact_issues:
            compact_issues.append(issue[:180].rstrip(" .,;:") + ("…" if len(issue) > 180 else ""))
    data["issues"] = compact_issues[:3]
    data["has_homework"] = any(bool(item.get("homework_present", True)) and str(item.get("task", "")).strip() for item in data["items"])
    data["needs_parent_approval"] = bool(data["issues"])
    data["summary"] = " ".join(str(data.get("summary") or "").split())[:400]
    if data["issues"]:
        data["summary"] = (data["summary"] + " Требуется подтверждение родителя.").strip()[:400]
    return DiaryResult.model_validate(data)

def check_homework(
    photo_paths: list[str],
    subject: str = "Авто",
) -> HomeworkResult:

    if not photo_paths:
        return HomeworkResult(
            is_homework=True,
            correct=False,
            confidence=1.0,
            correctness_percent=0,
            subject=subject,
            content_type="домашнее задание",
            summary="Фотографии домашнего задания отсутствуют.",
            mistakes=[
                "Не удалось получить фотографии домашнего задания."
            ],
            recommendation=(
                "Отправь фотографии домашнего задания ещё раз."
            ),
        )

    subject_instruction = (
        subject
        if subject and subject != "Авто"
        else "предмет не определён, определи его самостоятельно"
    )

    print(
        f"[AI CHECK] OpenRouter / Qwen3-VL-32B: "
        f"{len(photo_paths)} photos, subject={subject_instruction}"
    )

    # --------------------------------------------------------
    # PASS 1
    # --------------------------------------------------------

    recognition = _pass_one_recognition(
        photo_paths,
        subject_instruction,
    )

    # --------------------------------------------------------
    # PASS 2
    # --------------------------------------------------------

    correctness = _pass_two_check(
        photo_paths,
        subject_instruction,
        recognition,
    )

    # --------------------------------------------------------
    # PASS 3
    # --------------------------------------------------------

    audit = _pass_three_audit(
        photo_paths,
        subject_instruction,
        recognition,
        correctness,
    )

    # --------------------------------------------------------
    # FINAL MERGE
    # --------------------------------------------------------

    is_homework = bool(
        audit.get(
            "is_homework",
            correctness.get(
                "is_homework",
                recognition.get(
                    "is_homework",
                    True,
                ),
            ),
        )
    )

    confidence_values = []

    for source in (
        recognition,
        correctness,
        audit,
    ):
        try:
            confidence_values.append(
                float(
                    source.get(
                        "confidence",
                        source.get(
                            "recognition_confidence",
                            0.0,
                        ),
                    )
                )
            )
        except Exception:
            pass

    if confidence_values:
        confidence = min(confidence_values)
    else:
        confidence = 0.0

    # --------------------------------------------------------
    # COMBINE ALL ERRORS
    # --------------------------------------------------------

    all_mistakes = []

    for source in (
        correctness,
        audit,
    ):
        mistakes = source.get("mistakes", [])

        if isinstance(mistakes, list):
            for mistake in mistakes:
                mistake = str(mistake).strip()

                if mistake and mistake not in all_mistakes:
                    all_mistakes.append(mistake)

    # --------------------------------------------------------
    # PARENT APPROVAL
    # --------------------------------------------------------

    parent_requests = _merge_parent_requests(
        recognition,
        correctness,
        audit,
    )

    needs_parent_approval = bool(parent_requests)

    # --------------------------------------------------------
    # PERCENT
    # --------------------------------------------------------

    try:
        audit_percent = int(
            audit.get(
                "correctness_percent",
                0,
            )
        )
    except Exception:
        audit_percent = 0

    try:
        correctness_percent = int(
            correctness.get(
                "correctness_percent",
                audit_percent,
            )
        )
    except Exception:
        correctness_percent = audit_percent

    # Никогда не повышаем результат за счёт одного прохода.
    correctness_percent = min(
        audit_percent,
        correctness_percent,
    )

    correctness_percent = max(
        0,
        min(
            100,
            correctness_percent,
        ),
    )

    # --------------------------------------------------------
    # NON-HOMEWORK
    # --------------------------------------------------------

    if not is_homework:
        return HomeworkResult(
            is_homework=False,
            correct=False,
            confidence=confidence,
            correctness_percent=0,
            subject=subject,
            content_type="не домашнее задание",
            summary=(
                "Изображение не распознано как домашнее задание."
            ),
            mistakes=all_mistakes or [
                "Изображение не распознано как домашнее задание."
            ],
            recommendation=(
                "Отправь фотографию домашнего задания."
            ),
            needs_parent_approval=False,
            parent_approval_requests=[],
        )

    # --------------------------------------------------------
    # UNCERTAIN → NEVER 100%
    # --------------------------------------------------------

    if needs_parent_approval:
        correctness_percent = min(
            correctness_percent,
            99,
        )

    # --------------------------------------------------------
    # ANY ERROR → NOT CORRECT
    # --------------------------------------------------------

    correct = (
        is_homework
        and correctness_percent == 100
        and not all_mistakes
        and not needs_parent_approval
        and confidence >= 0.85
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    if needs_parent_approval:
        summary = (
            "AI нашёл одно или несколько мест, "
            "которые невозможно надёжно распознать. "
            "Нужно подтверждение родителя."
        )

        recommendation = (
            "Нужно подтверждение родителя по спорным местам."
        )

    elif all_mistakes:
        summary = (
            f"Найдено ошибок: {len(all_mistakes)}."
        )

        recommendation = (
            "Исправь указанные ошибки и отправь работу повторно."
        )

    elif correct:
        summary = (
            "Все задания распознаны и проверены тремя проходами. "
            "Ошибок не найдено."
        )

        recommendation = (
            "Работа выполнена правильно."
        )

    else:
        summary = (
            "Работа требует дополнительной проверки."
        )

        recommendation = (
            "Проверь работу и отправь её повторно."
        )

    # --------------------------------------------------------
    # FINAL SAFETY
    # --------------------------------------------------------

    if all_mistakes:
        correct = False

    if needs_parent_approval:
        correct = False

    if correctness_percent != 100:
        correct = False

    if confidence < 0.85:
        correct = False

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    result = HomeworkResult(
        is_homework=True,
        correct=correct,
        confidence=max(
            0.0,
            min(
                1.0,
                confidence,
            ),
        ),
        correctness_percent=correctness_percent,
        subject=subject,
        content_type="домашнее задание",
        summary=summary,
        mistakes=all_mistakes,
        recommendation=recommendation,
        needs_parent_approval=needs_parent_approval,
        parent_approval_requests=parent_requests,
    )

    print("===== FINAL HOMEWORK RESULT =====")
    print(
        json.dumps(
            result.model_dump(),
            ensure_ascii=False,
            indent=2,
        )
    )
    print("=================================")

    return result


# ============================================================
# SIMPLE CHAT → GROQ
# ============================================================

def legacy_chat_with_ai(
    user_message: str,
) -> str:

    client = _get_chat_client()

    system_prompt = """
Ты дружелюбный помощник школьника.

Отвечай на русском языке.
Объясняй понятно и относительно кратко.
Если пользователь задаёт учебный вопрос —
помогай разобраться, а не просто выдавай ответ без объяснения.

Не утверждай то, в чём не уверен.
"""

    try:
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_message,
                },
            ],
            temperature=0.3,
            max_tokens=1000,
        )

    except Exception as exc:
        raise RuntimeError(
            f"Ошибка Groq: {exc}"
        ) from exc

    if not response.choices:
        raise RuntimeError(
            "Groq не вернул choices"
        )

    message = response.choices[0].message

    if not message or not message.content:
        raise RuntimeError(
            "Groq вернул пустой ответ"
        )

    return message.content.strip()
