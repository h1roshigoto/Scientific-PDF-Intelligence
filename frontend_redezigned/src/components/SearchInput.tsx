import { Icon } from './Icon';

export function SearchInput({ value, update }: { value: string; update: (value: string) => void }) {
  return (
    <label className="search-field">
      <Icon name="search" size={15} />
      <input
        type="search"
        aria-label="Поиск по названию, DOI или ID"
        placeholder="Название статьи, DOI или ID"
        value={value}
        onChange={(event) => update(event.target.value)}
      />
    </label>
  );
}

export function FilterSelect({
  label,
  value,
  choices,
  onChange,
}: {
  label: string;
  value: string;
  choices: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  const options =
    value && !choices.some((choice) => choice.value === value)
      ? [...choices, { value, label: value }]
      : choices;
  return (
    <select
      className="select-control"
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">{label === 'Источник' ? label : `${label}: все`}</option>
      {options.map((entry) => (
        <option key={entry.value} value={entry.value}>
          {entry.label}
        </option>
      ))}
    </select>
  );
}
