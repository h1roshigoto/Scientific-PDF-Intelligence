# Паспорт данных Scientific PDF Intelligence

Проект извлекает сведения из научных PDF с привязкой к первоисточнику. Solar описывает материалы, устройства, слои, условия измерения и показатели тонкоплёночной фотовольтаики. RAG описывает системы Retrieval-Augmented Generation, конфигурации, наборы данных, задачи, протоколы оценки и результаты сравнения.

## Версии и источники

| Трек | Версия в SPI | Статус |
| --- | --- | --- |
| Solar | `solar-package-2026-11-20` | Идентификатор SPI для официального участнического архива области 1. Поля и порядок записаны по `TABLES` в его `tools/check_submission.py`, словари — по `VOCAB`. В оглавлении архива видны семь CSV и README. Прямая сверка содержимого CSV и README пока не выполнена. [Пакет Solar](https://disk.yandex.ru/d/V07H0cpOfDl4nA) |
| RAG | `rag-viniti-adaptation-1.0` | Заголовки, ключи и закрытые словари подтверждены по участническому комплекту. Его `schema.json` устанавливает `official_rag_template: false`. Это рекомендуемый машинный формат адаптации, связанный с предметными полями и правилами кейса. [Пакет RAG](https://drive.google.com/drive/folders/1i5E32-izTrvRdA1tVvBVpZqPzDsXe653?usp=sharing), [шаблон RAG](https://drive.google.com/drive/folders/1itlqF0IHP2Pa19HWEnHvWDKav7j4whqm) |

## Solar

Поля перечислены в порядке CSV.

| Файл | Поля |
| --- | --- |
| `publications.csv` | `publication_id`, `title`, `year`, `DOI`, `authors`, `in_scope`, `exclusion_reason` |
| `materials.csv` | `publication_id`, `material_id`, `material_name_raw`, `formula_normalized`, `material_class`, `dimensionality`, `additives`, `bandgap_value`, `bandgap_unit`, `bandgap_method`, `note` |
| `devices.csv` | `publication_id`, `device_id`, `absorber_material_id`, `device_label`, `device_role`, `device_type`, `architecture`, `substrate_type`, `active_area`, `area_unit`, `area_definition`, `encapsulation`, `note` |
| `layers.csv` | `publication_id`, `layer_id`, `device_id`, `layer_order`, `layer_function`, `layer_material`, `layer_thickness`, `thickness_unit`, `deposition_method`, `annealing_temperature`, `annealing_time`, `atmosphere`, `note` |
| `measurements.csv` | `publication_id`, `measurement_id`, `device_id`, `measurement_type`, `spectrum`, `light_intensity`, `intensity_unit`, `scan_direction`, `scan_rate`, `mask_used`, `certified`, `certification_lab`, `temperature`, `stability_protocol`, `stress_conditions`, `note` |
| `results.csv` | `publication_id`, `result_id`, `device_id`, `measurement_id`, `material_id`, `metric_name`, `metric_value`, `metric_unit`, `aggregation`, `spread_value`, `n_devices`, `time_point`, `time_unit`, `raw_value`, `raw_unit`, `normalized_value`, `normalized_unit`, `result_origin`, `cited_publication_id`, `source_page`, `source_section`, `source_table`, `source_figure`, `source_fragment`, `extraction_status`, `duplicate_of`, `note` |
| `issues.csv` | `publication_id`, `issue_id`, `record_ids`, `check_name`, `description`, `source_page`, `source_fragment` |

| Файл | Локальный ключ | Внешние ключи той же публикации |
| --- | --- | --- |
| `materials.csv` | `material_id` | |
| `devices.csv` | `device_id` | `absorber_material_id` → materials |
| `layers.csv` | `layer_id` | `device_id` → devices |
| `measurements.csv` | `measurement_id` | `device_id` → devices, список через `;` |
| `results.csv` | `result_id` | `device_id` → devices, `measurement_id` → measurements, `material_id` → materials, `duplicate_of` → results |
| `issues.csv` | `issue_id` | `record_ids` → results, список через `;` |

| Поле | Допустимые значения |
| --- | --- |
| `material_class` | `perovskite`, `CIGS`, `CdTe`, `kesterite`, `Sb2Se3`, `thin-film Si`, `other` |
| `dimensionality` | `3D`, `2D`, `2D-3D` |
| `bandgap_method` | `Tauc`, `EQE`, `PL`, `cited` |
| `device_role` | `control`, `target`, `record` |
| `device_type` | `cell`, `minimodule`, `module` |
| `architecture` | `n-i-p`, `p-i-n`, `substrate`, `superstrate` |
| `substrate_type` | `rigid`, `flexible` |
| `area_definition` | `designated illumination`, `aperture`, `total` |
| `layer_function` | `front_contact`, `ETL`, `absorber`, `passivation`, `HTL`, `interlayer`, `recombination`, `back_contact` |
| `measurement_type` | `J-V`, `MPP tracking`, `EQE`, `stability` |
| `scan_direction` | `reverse`, `forward`, `average` |
| `stability_protocol` | `ISOS-D`, `ISOS-L`, `ISOS-T`, `other` |
| `aggregation` | `champion`, `mean`, `median` |
| `mask_used`, `certified` | `true`, `false` |

SPI проверяет `layer_order` и `n_devices` как целые числа. `bandgap_value`, `active_area`, `layer_thickness`, `light_intensity`, `metric_value`, `spread_value`, `time_point` и `normalized_value` проверяются как числа с десятичной точкой. Единицы указываются в сопряжённых полях `bandgap_unit`, `area_unit`, `thickness_unit`, `intensity_unit`, `metric_unit`, `time_unit` и `normalized_unit`. Примеры величин и единиц включают PCE в процентах, `Jsc` в мА/см², `Voc` в В, ширину запрещённой зоны в эВ, толщину в нм и время в часах. `scan_rate`, `temperature`, `annealing_temperature` и `annealing_time` могут содержать исходное значение с единицей и условиями. Указывайте вид площади, протокол стабильности и контекст измерения, когда они следуют из статьи. Полные правила нормализации и записи единиц из Solar README ещё предстоит сверить.

## RAG

| Файл | Поля в порядке CSV |
| --- | --- |
| `publications.csv` | `publication_id`, `title`, `year`, `DOI`, `authors`, `in_scope`, `exclusion_reason` |
| `configurations.csv` | `publication_id`, `config_id`, `rag_variant`, `retrieval_method`, `retriever_model`, `embedding_model`, `embedding_version`, `chunking_method`, `chunk_size`, `chunk_overlap`, `chunk_unit`, `index_type`, `vector_store`, `top_k`, `reranker_model`, `generator_model`, `generator_version`, `prompt_strategy`, `context_strategy`, `note` |
| `datasets.csv` | `publication_id`, `dataset_id`, `dataset_name`, `dataset_version`, `language`, `data_split`, `scientific_domain`, `note` |
| `tasks.csv` | `publication_id`, `task_id`, `task_type`, `note` |
| `experiments.csv` | `publication_id`, `experiment_id`, `evaluation_protocol`, `baseline_name`, `hardware`, `latency_ms`, `tokens_per_query`, `sample_size`, `note` |
| `results.csv` | `publication_id`, `result_id`, `experiment_id`, `config_id`, `dataset_id`, `task_id`, `metric_group`, `metric_name`, `metric_variant`, `metric_direction`, `metric_k`, `metric_value`, `metric_unit`, `baseline_value`, `aggregation`, `sample_size`, `raw_value`, `raw_unit`, `normalized_value`, `normalized_unit`, `result_origin`, `cited_publication_id`, `source_page`, `source_section`, `source_table`, `source_figure`, `source_fragment`, `extraction_status`, `duplicate_of`, `note` |
| `issues.csv` | `publication_id`, `issue_id`, `record_ids`, `check_name`, `description`, `source_page`, `source_fragment` |

| Файл | Локальный ключ | Внешние ключи той же публикации |
| --- | --- | --- |
| `configurations.csv` | `config_id` | |
| `datasets.csv` | `dataset_id` | |
| `tasks.csv` | `task_id` | |
| `experiments.csv` | `experiment_id` | |
| `results.csv` | `result_id` | `experiment_id` → experiments, `config_id` → configurations, `dataset_id` → datasets, `task_id` → tasks, `duplicate_of` → results |
| `issues.csv` | `issue_id` | `record_ids` → results, список через `;` |

`top_k`, `metric_k` и `sample_size` — неотрицательные целые. Нулевой `top_k` допустим для сравнения без извлечённого контекста. `chunk_size` и `chunk_overlap` — неотрицательные числа в указанной `chunk_unit`, например в токенах или символах. `latency_ms` хранится в миллисекундах, `tokens_per_query` — в токенах на запрос. `metric_value`, `normalized_value` и `baseline_value` — числа на подтверждённой шкале. Примеры единиц `fraction`, `%`, `ms`, `s`. При заполненном `normalized_value` единицы `metric_unit` и `normalized_unit` совпадают. Заполненный `metric_value` равен `normalized_value` по числовому значению.

Рекомендуемые значения `metric_direction` — `higher_is_better`, `lower_is_better`, `target`. Неизвестное направление остаётся пустым. Здесь `aggregation` и предметные названия остаются открытыми полями. Сравнение с baseline требует сопоставимости набора, задачи, определения метрики, шкалы и протокола. Доли и проценты, а также шкалы BLEU и ROUGE определяются по источнику. Параметры, опубликованные диапазоном, описываются в `note` с пустой числовой ячейкой.

## Общие типы, идентификаторы и источники

- `publications.publication_id` — строковый первичный ключ с именем файла без `.pdf` и без внутреннего префикса трека. В остальных таблицах `publication_id` — внешний ключ на publications. Первичный ключ каждой зависимой строки составной, из `publication_id` и локального ключа из таблиц выше. `cited_publication_id` хранит обозначение из библиографии статьи.
- Каждый PDF из проверяемой папки представлен одной строкой. `in_scope` — `true` или `false`. `exclusion_reason` обязательно при `false` и пусто при `true`. Исключение определяется предметными правилами трека. В RAG у исключённой публикации нет зависимых строк. SPI допускает для исключённых публикаций Solar замечания уровня публикации.
- `year` и `source_page` — положительные целые. Поля описания, названия, идентификаторы, исходные значения и единицы хранятся как текст, если для поля не указан другой тип. Идентификаторы не преобразуются в числа. Авторы и списки ссылок разделяются `;`.
- `result_origin` принимает `primary` для результатов, полученных в самой работе, и `cited` для значений, перенесённых из других работ. Для `cited` требуется ссылка в `cited_publication_id`.
- Порядок колонок, регистр заголовков и имя таблицы совпадают с шаблоном трека. Формат — UTF-8, запятая, десятичная точка, стандартное экранирование CSV. Во внутреннем обмене отсутствующее значение — JSON `null` или Python `None`. В CSV это пустая ячейка. Ноль и логическое `false` сохраняются.
- `raw_value` и `raw_unit` сохраняют обозначения и точность автора. Нормализованные значение и единица хранятся отдельно. Нормализация требует установленной шкалы и контекста. Экспорт проверяет представление чисел и сохраняет переданные пары. Формулы и метод пересчёта документируются в `note`, если без них результат неоднозначен.
- `source_page` указывает на страницу файла PDF с единицы. `source_fragment` хранит фрагмент этой страницы, `source_table` и `source_figure` — метку таблицы или рисунка, `source_section` — раздел. Строку, колонку, кривую или способ оцифровки можно уточнить в `note`. Для `extracted` нужны исходное значение, страница и хотя бы один из трёх локаторов — фрагмент, таблица или рисунок.
- `extraction_status` участнического CSV принимает `extracted`, `present_not_extractable`, `absent`. Не заполненная разметка сама по себе не является наблюдением об отсутствии метрики. Указание `duplicate_of` сохраняет отдельный источник повторного упоминания и ссылается на результат той же публикации. Циклы запрещены.

## Замечания и статусы SPI

Каждое замечание содержит уникальный в публикации `issue_id`, название проверки `check_name` и содержание `description`. В `record_ids` указываются затронутые `result_id`. Для замечания уровня публикации список может быть пустым. При наличии цитаты указывается страница. Примеры проверок — несовпадение повторных значений, несопоставимые режимы измерения или протоколы оценки, вопрос к единице и масштабу. Предметные замечания передаёт этап валидации данных.

Технический `ocr_status` страницы SPI принимает `not_needed` или `pending` на этапе импорта. Внутренний результат API отдельно хранит `extraction_status` со значениями `extracted`, `needs_review`, `verified`. Это состояние разметки и проверки внутри приложения. Экспорт использует словарь трека и требует явного решения автора разметки для преобразования внутренних состояний. Успешное чтение текста страницы само по себе не подтверждает научное значение.

## Использование

На вход подаётся JSON с полями `domain` и `tables`. В `tables` ровно семь имён таблиц без `.csv`, для каждой передаётся массив объектов. Например, `"publications": [{"publication_id": "P001", "in_scope": false, "exclusion_reason": "Причина из разметки"}]`; пустые таблицы задаются как `[]`.

```sh
python -m spi.csv_export solar outputs/solar.json outputs/solar-submission --pdf-dir data/solar/test
python -m spi.csv_export rag outputs/rag.json outputs/rag-submission --pdf-dir data/rag/test --template-dir data/rag/submission_template
```

`--template-dir` сверяет исходные заголовки и доступный `schema.json` с закреплённой версией. При недоступном каталоге экспорт завершается ошибкой. При вызове без этого параметра используются закреплённые определения SPI. Каталог назначения создаётся целиком после валидации. Для проверки существующих CSV используйте `read_submission` и `validate_submission`, передав список имён PDF без расширения.

## Ограничения этапа

Структурная валидация SPI проверяет ссылки, уникальность и покрытие переданного набора PDF. Проверка цитаты на странице, значение и единица в строке таблицы, сопоставимость контекста и полнота научного извлечения требуют проверки скриптами участников и анализа источника. CSV одинаковой структуры могут содержать значения не из того корпуса, поэтому формирование входного JSON и выбор списка PDF остаются обязанностью вызывающей стороны.

Заголовки всех семи RAG CSV совпали с `schema.json` и заголовками `dev_gold`. Оба участнических `check_submission.py` успешно проверили синтетическую выгрузку с двумя PDF на трек и двумя цитатами. Эталонные научные значения, OCR и полнота извлечения в этих запусках не оценивались.

На следующем этапе нужно сверить оригинальные CSV, README и `dev_gold` из Solar с контрактом, согласовать отображение внутренних записей SPI на предметные строки и разметить контрольные PDF обоих треков. Для RAG следует уточнить у координатора статус адаптации для итоговой сдачи.
