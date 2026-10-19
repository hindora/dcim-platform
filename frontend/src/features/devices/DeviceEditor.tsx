import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ApiError, api, atLeast,
  type DeviceDetail, type DevicePatch,
} from '../../api/client';
import { humanise } from '../../lib/format';

/** Editing what a device IS on record.
 *
 *  Two tiers, by what a wrong value costs. An operator sets the operational
 *  facts: who owns it, what it costs, how much it matters, a note. Identity -
 *  name, make, model, serial, asset tag and both addresses - is what
 *  discovery reconciles on and the collector dials, so it needs an admin; for
 *  everyone else those fields are shown, not hidden, with the reason.
 *
 *  An identity field set here stays set across a re-import (the importer
 *  skips a field named in `locked_fields`); an admin can hand it back. */
type Form = Record<keyof Required<DevicePatch>, string>;

const IDENTITY = ['name', 'vendor', 'model', 'serial_number', 'asset_tag',
                  'mgmt_ip', 'primary_ip'] as const;
const LOCKABLE = new Set(['name', 'vendor', 'model', 'serial_number', 'mgmt_ip', 'primary_ip']);

const start = (d: DeviceDetail): Form => ({
  name: d.name,
  vendor: d.vendor ?? '',
  model: d.model ?? '',
  serial_number: d.serial_number ?? '',
  asset_tag: d.asset_tag ?? '',
  mgmt_ip: d.mgmt_ip ?? '',
  primary_ip: d.primary_ip ?? '',
  owner_group: d.owner_group ?? '',
  cost_centre: d.cost_centre ?? '',
  priority: d.priority ?? 'normal',
  notes: d.notes ?? '',
});

const IPV4 = /^(\d{1,3})(\.\d{1,3}){3}$/;

function validate(f: Form): Partial<Record<keyof Form, string>> {
  const e: Partial<Record<keyof Form, string>> = {};
  if (!f.name.trim()) e.name = 'A device needs a name.';
  for (const k of ['mgmt_ip', 'primary_ip'] as const) {
    const v = f[k].trim();
    if (v && !(IPV4.test(v) && v.split('.').every((o) => Number(o) <= 255)) && !v.includes(':')) {
      e[k] = 'Not an IP address.';
    }
  }
  if (f.model.trim() && !f.vendor.trim()) e.model = 'A model belongs to a vendor: set the vendor too.';
  return e;
}

export function DeviceEditor({ device, onClose }: { device: DeviceDetail; onClose: () => void }) {
  const qc = useQueryClient();
  const isAdmin = atLeast('admin');
  const [form, setForm] = useState<Form>(() => start(device));
  const base = useMemo(() => start(device), [device]);
  const errors = useMemo(() => validate(form), [form]);

  const patch = useMemo(() => {
    const p: Record<string, string> = {};
    for (const k of Object.keys(form) as (keyof Form)[]) {
      if (form[k].trim() !== base[k].trim()) p[k] = form[k].trim();
    }
    return p as DevicePatch;
  }, [form, base]);
  const changed = Object.keys(patch).length;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['device', device.id] });
    qc.invalidateQueries({ queryKey: ['devices'] });
    qc.invalidateQueries({ queryKey: ['twin-scene'] });
  };
  const save = useMutation({
    mutationFn: () => api.editDevice(device.id, patch),
    onSuccess: () => { refresh(); onClose(); },
  });
  const unlock = useMutation({
    mutationFn: (fields: string[]) => api.unlockDevice(device.id, fields),
    onSuccess: refresh,
  });

  const set = (k: keyof Form, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const locked = new Set(device.locked_fields ?? []);

  const field = (k: keyof Form, label: string, opts: {
    mono?: boolean; hint?: string; placeholder?: string } = {}) => {
    const identity = (IDENTITY as readonly string[]).includes(k);
    const off = identity && !isAdmin;
    return (
      <label key={k}>
        <span>{label}{locked.has(k) && <small className="lock-chip" title="Set by hand: a re-import leaves it"> set by hand</small>}</span>
        <input value={form[k]} disabled={off} className={opts.mono ? 'mono' : undefined}
               placeholder={opts.placeholder}
               onChange={(e) => set(k, e.target.value)} />
        <em className={errors[k] ? 'hint bad' : 'hint'}>
          {errors[k] ?? (off ? 'Identity: changing it needs an admin account.' : opts.hint)}
        </em>
      </label>
    );
  };

  const err = save.error ?? unlock.error;
  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={`Edit ${device.name}`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet narrow">
        <header className="sheet-head">
          <div>
            <h2>Edit {humanise(device.device_type)}</h2>
            <p>
              What this device is on record. Identity fields stay as you set
              them when the inventory is re-imported from the simulator.
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="sheet-body">
          {err && (
            <div className="banner" role="alert">
              {err instanceof ApiError && err.status === 403 && !isAdmin
                ? 'That needs an admin account.' : String((err as Error).message)}
            </div>
          )}

          <h3 className="sheet-sub">Identity</h3>
          <div className="form-grid">
            {field('name', 'Name')}
            {field('vendor', 'Vendor', { hint: 'A new name creates the vendor.' })}
            {field('model', 'Model', { hint: 'Set together with the vendor.' })}
            {field('serial_number', 'Serial number', { mono: true,
              hint: 'Discovery reconciles on this: it must be the chassis serial.' })}
            {field('asset_tag', 'Asset tag', { mono: true, hint: 'The sticker on the chassis.' })}
            {field('mgmt_ip', 'Management IP', { mono: true, placeholder: '10.51.0.10',
              hint: 'The collector polls this address.' })}
            {field('primary_ip', 'Production IP', { mono: true })}
          </div>
          {isAdmin && locked.size > 0 && (
            <p className="muted">
              Set by hand: {[...locked].map(humanise).join(', ')}.{' '}
              <button className="linklike" disabled={unlock.isPending}
                      onClick={() => unlock.mutate([...locked].filter((k) => LOCKABLE.has(k)))}>
                Hand them back to the import
              </button>
            </p>
          )}

          <h3 className="sheet-sub">Operations</h3>
          <div className="form-grid">
            {field('owner_group', 'Owner', { hint: 'The team that answers for it.' })}
            {field('cost_centre', 'Cost centre')}
            <label>
              <span>Priority</span>
              <select value={form.priority} onChange={(e) => set('priority', e.target.value)}>
                <option value="high">High: page for any problem</option>
                <option value="normal">Normal</option>
                <option value="low">Low: can wait for the morning</option>
              </select>
              <em className="hint">How much it matters when something is wrong with it.</em>
            </label>
            <label>
              <span>Notes</span>
              <textarea rows={3} value={form.notes} onChange={(e) => set('notes', e.target.value)} />
            </label>
          </div>
        </div>

        <div className="sheet-foot">
          <span className="muted">
            {changed ? `${changed} change${changed === 1 ? '' : 's'} to save` : 'Nothing changed'}
          </span>
          <span className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary"
                  disabled={!changed || Object.keys(errors).length > 0 || save.isPending}
                  onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </section>
    </div>
  );
}
