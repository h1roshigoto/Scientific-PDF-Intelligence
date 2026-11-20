import { useCallback } from 'react';
import { contextLabel, contextText, publicationId } from './domain';
import type { Domain, Issue, Result } from './domain';
import { documentSourceUrl, Link, sectionUrl } from './navigation';
import { provider } from './provider';
import { checkLabel, checkValues, evidenceLinks, orderedLayers, publicationScope } from './review';
import { Badge, InfoList, LoadBoundary, useData } from './ui';

export function useReviewIssues(domain: Domain) {
  const load = useCallback((signal: AbortSignal) => provider.issues(domain, signal), [domain]);
  return useData(load);
}

export function ReviewSources({ issue }: { issue: Issue }) {
  return evidenceLinks(issue).map(({ documentId, page, label }, index) => <Link key={`${documentId}-${page}-${index}`}
    href={documentSourceUrl('checks', issue.domain, documentId, page, issue.issue_id)}>
    {publicationId(documentId)} · стр. {page}{label && ` · ${label}`}
  </Link>);
}

export function ReviewEntries({ issues }: { issues: Issue[] }) {
  if (!issues.length) return <p className="muted">Для этой записи сведений о проверках пока нет.</p>;
  return <ul className="review-entries">{issues.map((issue) => {
    const values = checkValues(issue);
    const sources = evidenceLinks(issue);
    const scope = sources[0] && publicationScope([issue], sources[0].documentId);
    const duplicate = issue.rule_id === 'confirmed_duplicate' || issue.rule_id === 'suspected_duplicate';
    return <li key={issue.issue_id}>
      <h3><Link href={sectionUrl('checks', issue.domain, undefined, issue.issue_id)}>{duplicate
        ? <Badge tone={issue.rule_id === 'confirmed_duplicate' ? 'info' : 'warn'}>{checkLabel(issue)}</Badge>
        : checkLabel(issue)}</Link></h3>
      {scope && <Badge tone={scope.tone}>{scope.label}</Badge>}
      <p>{issue.reason}</p>
      {values.length > 0 && !scope && <dl>{values.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
      {sources.length > 0 && <div className="issue-sources"><ReviewSources issue={issue} /></div>}
    </li>;
  })}</ul>;
}

export function ScientificReview({ domain, recordId, documentId }: { domain: Domain; recordId?: string; documentId: string }) {
  const issues = useReviewIssues(domain);
  return <section className="panel inspector scientific-review" aria-labelledby="scientific-review-title">
    <h2 id="scientific-review-title">{recordId ? 'Проверки записи' : 'Допуск и доступность данных'}</h2>
    <LoadBoundary {...issues}>{(rows) => <ReviewEntries issues={rows.filter((issue) => recordId
      ? issue.result_ids.includes(recordId) || (!issue.result_ids.length && evidenceLinks(issue).some((link) => link.documentId === documentId))
      : !issue.result_ids.length && evidenceLinks(issue).some((link) => link.documentId === documentId))} />}</LoadBoundary>
  </section>;
}

const groups = [
  { label: 'Материал и устройство', keys: ['material_name_raw', 'formula_normalized', 'material_class', 'bandgap_value', 'bandgap_unit', 'device_role', 'device_type', 'architecture', 'substrate_type', 'active_area', 'area_definition'] },
  { label: 'Условия измерения', keys: ['spectrum', 'light_intensity', 'intensity_unit', 'scan_rate', 'mask_used', 'temperature', 'measurement_instrument', 'stability_protocol', 'stress_conditions'] },
] as const;
const layerKeys = ['layer_function', 'layer_material', 'layer_thickness', 'thickness_unit', 'deposition_method', 'annealing_temperature', 'annealing_time', 'atmosphere'];

export function solarStructureField(key: string, value: unknown) {
  return key === 'layers' ? orderedLayers(value).length > 0
    : groups.some((group) => group.keys.some((field) => key === field));
}

export function SolarStructure({ result }: { result: Result }) {
  if (result.domain !== 'solar') return null;
  const { context } = result;
  const layers = orderedLayers(context.layers);
  return <details className="panel solar-structure">
    <summary>Устройство, слои и измерение</summary>
    {groups.map(({ label, keys }) => <section key={label}>
      <h3>{label}</h3>
      <InfoList items={keys.map((key) => [contextLabel(key), contextText(context[key])])} />
    </section>)}
    <section><h3>Порядок слоёв</h3>
      {layers.length ? layers.map((layer, index) => {
        const extra = Object.keys(layer).filter((key) => key !== 'layer_order' && !layerKeys.includes(key));
        return <div className="layer-entry" key={index}>
          <h4>Слой {contextText(layer.layer_order)}</h4>
          <InfoList items={[...layerKeys, ...extra].map((key) => [contextLabel(key), contextText(layer[key])])} />
        </div>;
      }) : <p className="muted">Сведения о слоях не переданы.</p>}
    </section>
  </details>;
}
