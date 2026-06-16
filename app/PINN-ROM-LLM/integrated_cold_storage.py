#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import glob
import json
import shutil
import argparse
import csv
import math
import datetime
from types import SimpleNamespace
from typing import List, Dict, Any, Optional

import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
import gradio as gr

# ==========================================
# ГЛОБАЛЬНЫЕ КОНСТАНТЫ И УТИЛИТЫ
# ==========================================

OUT_DIR = "out"
os.makedirs(OUT_DIR, exist_ok=True)

CSV_COLS = {
    "hour": "hour",
    "T": "T_in_C",
    "RH": "RH_in",
    "n_in": "n_in",
    "n_out": "n_out",
    "door_ev": "door_events",
    "E_el_tot": "E_el_total_kWh",
}

def set_seed(seed=42):
    np.random.seed(seed)
    torch.manual_seed(seed)

def to_tensor(x, device="cpu", dtype=torch.float32):
    return torch.tensor(x, device=device, dtype=dtype)

def parse_csv_floats(s):
    if isinstance(s, (list, tuple)): return [float(v) for v in s]
    if s is None: return None
    s = str(s).strip()
    if s == "": return None
    return [float(x) for x in s.split(",") if str(x).strip() != ""]

def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def fmt(v, nd=2) -> str:
    if v is None: return "null"
    try: return f"{float(v):.{nd}f}"
    except Exception: return str(v)

def pct(x, q):
    return float(np.percentile(x, q)) if len(x) > 0 else None

# ==========================================
# ФИЗИКА И ТЕПЛОПРОВОДНОСТЬ
# ==========================================

def effective_wall_from_layers(d_list, k_list, rho_list, cp_list):
    d = np.array(d_list, dtype=float)
    k = np.array(k_list, dtype=float)
    rho = np.array(rho_list, dtype=float)
    cp = np.array(cp_list, dtype=float)
    L = float(d.sum())
    if L <= 0: raise ValueError("Total thickness must be > 0")
    keff = L / np.sum(d / k)
    rho_eff = float(np.sum(rho * d) / L)
    rho_cp_eff = float(np.sum(rho * cp * d) / L)
    cp_eff = rho_cp_eff / rho_eff
    return L, keff, rho_eff, cp_eff

def build_1d_decomposed(Nx, Lx, k, rho, cp):
    dx = Lx / (Nx - 1)
    alpha = k / (rho * cp)
    inv_dx2 = 1.0 / (dx * dx)
    L0 = np.zeros((Nx, Nx), dtype=np.float64)
    for i in range(1, Nx - 1):
        L0[i, i - 1] = inv_dx2
        L0[i, i] = -2.0 * inv_dx2
        L0[i, i + 1] = inv_dx2
    L0[0, 0] = -2.0 * inv_dx2
    L0[0, 1] = 2.0 * inv_dx2
    L0[-1, -1] = -2.0 * inv_dx2
    L0[-1, -2] = 2.0 * inv_dx2
    L_L = np.zeros_like(L0)
    L_R = np.zeros_like(L0)
    L_L[0, 0] = -2.0 * inv_dx2
    L_R[-1, -1] = -2.0 * inv_dx2
    S_L = np.zeros(Nx, dtype=np.float64)
    S_R = np.zeros(Nx, dtype=np.float64)
    S_L[0] = 2.0 * inv_dx2
    S_R[-1] = 2.0 * inv_dx2
    return alpha, dx, L0, L_L, L_R, S_L, S_R

def build_1d_with_h(Nx, Lx, k, rho, cp, hL, hR):
    alpha, dx, L0, L_L, L_R, S_L, S_R = build_1d_decomposed(Nx, Lx, k, rho, cp)
    BiL = hL * dx / k
    BiR = hR * dx / k
    L = L0 + BiL * L_L + BiR * L_R
    S_left = BiL * S_L
    S_right = BiR * S_R
    return alpha, dx, L, S_left, S_right

def build_1d_with_h_wrapper(Nx, Lx, k, rho, cp, hL, hR):
    return build_1d_with_h(Nx, Lx, k, rho, cp, hL, hR)

def theta_step_build(I, L, alpha, dt, theta, Tn, s_old, s_new):
    A = I - theta * dt * alpha * L
    rhs = (I + (1.0 - theta) * dt * alpha * L) @ Tn + dt * alpha * (theta * s_new + (1.0 - theta) * s_old)
    return A, rhs

def theta_integrate_1d(T0, times, alpha, L, S_left, S_right, TinfL_fn, TinfR_fn, theta=0.55):
    Nx = T0.shape[0]
    I = np.eye(Nx)
    T = T0.copy()
    snaps = [T.copy()]
    s_old = S_left * TinfL_fn(times[0]) + S_right * TinfR_fn(times[0])
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        s_new = S_left * TinfL_fn(times[i]) + S_right * TinfR_fn(times[i])
        A, b = theta_step_build(I, L, alpha, dt, theta, T, s_old, s_new)
        T = np.linalg.solve(A, b)
        snaps.append(T.copy())
        s_old = s_new
    return np.stack(snaps, axis=1)

# ==========================================
# МОДЕЛИ СНИЖЕНИЯ РАЗМЕРНОСТИ (ROM) И НЕЙРОННЫЕ СЕТИ
# ==========================================

def compute_pod(snapshot_matrix, r):
    mu = np.mean(snapshot_matrix, axis=1, keepdims=True)
    X = snapshot_matrix - mu
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    Phi = U[:, :r]
    A_snap = np.diag(S[:r]) @ Vt[:r, :]
    return mu.squeeze(), Phi, A_snap

def build_rom_decomposition(alpha, L0, L_L, L_R, S_L, S_R, Phi, mu):
    A0 = Phi.T @ (alpha * L0) @ Phi
    A_L = Phi.T @ (alpha * L_L) @ Phi
    A_R = Phi.T @ (alpha * L_R) @ Phi
    B_L_unit = Phi.T @ (alpha * S_L)
    B_R_unit = Phi.T @ (alpha * S_R)
    c0 = Phi.T @ (alpha * (L0 @ mu))
    c_L_unit = Phi.T @ (alpha * (L_L @ mu))
    c_R_unit = Phi.T @ (alpha * (L_R @ mu))
    return dict(A0=A0, A_L=A_L, A_R=A_R,
                B_L_unit=B_L_unit, B_R_unit=B_R_unit,
                c0=c0, c_L_unit=c_L_unit, c_R_unit=c_R_unit)

class MLP(nn.Module):
    def __init__(self, in_dim, out_dim, hidden=64, depth=3):
        super().__init__()
        layers = []
        d = in_dim
        for _ in range(depth):
            layers += [nn.Linear(d, hidden), nn.Tanh()]
            d = hidden
        layers.append(nn.Linear(d, out_dim))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)

def train_pinn_for_rom(A_rom, B_list, c_rom, a0, T_end, device='cpu',
                       steps=1500, lr=1e-3, n_colloc=256, t_power=1.0,
                       Tinf_time_fns=None, verbose=True):
    r = a0.shape[0]
    model = MLP(1, r, 64, 3).to(device)
    opt = optim.Adam(model.parameters(), lr=lr)

    def b_of_t(t_phys):
        if Tinf_time_fns is None or len(B_list) == 0:
            Bsum = torch.zeros((t_phys.shape[0], r), device=device)
        else:
            comps = []
            for B, f in zip(B_list, Tinf_time_fns):
                Bt = to_tensor(B, device=device).unsqueeze(0).repeat(t_phys.shape[0], 1)
                comps.append(Bt * f(t_phys).unsqueeze(1))
            Bsum = torch.stack(comps, 0).sum(0)
        c = to_tensor(c_rom, device=device).unsqueeze(0).repeat(t_phys.shape[0], 1)
        return Bsum + c

    for it in range(steps):
        t = torch.rand(n_colloc, 1, device=device) ** t_power
        t_phys = t * T_end
        t.requires_grad_(True)
        a = model(t)
        a_dot = torch.autograd.grad(a, t, grad_outputs=torch.ones_like(a),
                                    create_graph=True, retain_graph=True)[0]
        A = to_tensor(A_rom, device=device)
        rhs = (a @ A.T) + b_of_t(t_phys)
        res = a_dot - rhs
        loss_res = (res ** 2).mean()
        t0 = torch.zeros(1, 1, device=device)
        a0_pred = model(t0)
        loss_ic = ((a0_pred.squeeze(0) - to_tensor(a0, device=device)) ** 2).mean()
        loss = loss_res + 10.0 * loss_ic
        opt.zero_grad()
        loss.backward()
        opt.step()
        if verbose and (it + 1) % max(steps // 10, 1) == 0:
            print(f'[{it + 1}/{steps}] loss={loss.item():.3e}  res={loss_res.item():.3e}  ic={loss_ic.item():.3e}')
    return model

class ConductionROM1D:
    def __init__(self, Phi, mu, A_base=None, B_base=None, c_base=None,
                 A0=None, A_L=None, A_R=None, B_L_unit=None, B_R_unit=None,
                 c0=None, c_L_unit=None, c_R_unit=None,
                 idx_surface=0, A_inner=1.0, dx=None, k_eff=None):
        self.Phi = Phi
        self.mu = mu
        self.idx_surface = idx_surface
        self.A_inner = A_inner
        self.dx = dx
        self.k_eff = k_eff
        self.a = np.zeros(Phi.shape[1])
        self.A_base = A_base
        self.B_base = B_base
        self.c_base = c_base
        self.A0 = A0
        self.A_L = A_L
        self.A_R = A_R
        self.B_L_unit = B_L_unit
        self.B_R_unit = B_R_unit
        self.c0 = c0
        self.c_L_unit = c_L_unit
        self.c_R_unit = c_R_unit

    def has_decomposition(self):
        return (self.A0 is not None) and (self.A_L is not None) and (self.A_R is not None) \
            and (self.B_L_unit is not None) and (self.B_R_unit is not None) \
            and (self.c0 is not None) and (self.c_L_unit is not None) and (self.c_R_unit is not None) \
            and (self.dx is not None) and (self.k_eff is not None)

    def surface_T(self):
        T = self.mu + self.Phi @ self.a
        return float(T[self.idx_surface])

    def step_theta(self, dt, theta, Tin_K_old, Tout_K_old, Tin_K_new=None, Tout_K_new=None,
                   h_inner_curr=None, h_outer_curr=None,
                   h_inner_base=None, h_outer_base=None):
        if Tin_K_new is None: Tin_K_new = Tin_K_old
        if Tout_K_new is None: Tout_K_new = Tout_K_old
        I = np.eye(self.Phi.shape[1])
        if self.has_decomposition():
            BiL = (h_inner_curr * self.dx / self.k_eff) if h_inner_curr is not None else 0.0
            BiR = (h_outer_curr * self.dx / self.k_eff) if h_outer_curr is not None else 0.0
            A = self.A0 + BiL * self.A_L + BiR * self.A_R
            B_left = BiL * self.B_L_unit
            B_right = BiR * self.B_R_unit
            c = self.c0 + BiL * self.c_L_unit + BiR * self.c_R_unit
        else:
            fL = float((h_inner_curr or h_inner_base) / max(h_inner_base or 1.0, 1e-9))
            fR = float((h_outer_curr or h_outer_base) / max(h_outer_base or 1.0, 1e-9))
            A = self.A_base
            B_left = self.B_base[0] * fL
            B_right = self.B_base[1] * fR
            c = self.c_base
        Bold = B_left * Tin_K_old + B_right * Tout_K_old
        Bnew = B_left * Tin_K_new + B_right * Tout_K_new
        rhs = (I + (1 - theta) * dt * A) @ self.a + dt * ((1 - theta) * (Bold + c) + theta * (Bnew + c))
        self.a = np.linalg.solve(I - theta * dt * A, rhs)
        return self.a

# ==========================================
# СИМУЛЯТОР ЗОНЫ (ColdRoomZone)
# ==========================================

class ColdRoomZone:
    def __init__(self, room_L=10.0, room_W=10.0, room_H=4.0, T_set=2.0, RH_set=0.80,
                 T_bounds=(-0.5, 4.0), RH_bounds=(0.75, 0.90), airflow_bounds=(1000.0, 8000.0),
                 rho_air=1.2, cp_air=1006.0, p_atm=101325.0, h_fg=2.5e6, seed=123):
        set_seed(seed)
        self.L, self.W, self.H = room_L, room_W, room_H
        self.A_inner_total = 2 * (room_L * room_W + room_L * room_H + room_W * room_H)
        self.V = room_L * room_W * room_H
        self.rho = rho_air
        self.cp = cp_air
        self.p = p_atm
        self.h_fg = h_fg
        self.C_air = self.rho * self.V * self.cp
        self.T_set = T_set
        self.RH_set = RH_set
        self.T_bounds = T_bounds
        self.RH_bounds = RH_bounds
        self.airflow_bounds = airflow_bounds

    @staticmethod
    def sat_vapor_pressure(T_c): 
        return 610.78 * math.exp(17.2694 * T_c / (T_c + 237.3))

    def RH_to_w(self, T_c, RH):
        pws = self.sat_vapor_pressure(T_c)
        pw = RH * pws
        return 0.62198 * pw / (self.p - pw)

    def w_to_RH(self, T_c, w):
        pws = self.sat_vapor_pressure(T_c)
        pw = (w * self.p) / (0.62198 + w)
        RH = pw / pws
        return max(0.0, min(1.0, RH))

    def simulate(self, hours=8760,
                 T_out_series=None, RH_out_series=None, airflow_series=None, wind_series=None,
                 beef_mean_in=1.0, beef_mean_out=1.0,
                 beef_E_kWh_range=(5.6, 10.0), beef_use_weight=False, beef_weight_mu=350.0, beef_weight_sigma=50.0, beef_EkWh_per_kg=7.8 / 350.0,
                 beef_chill_hours=8,
                 cop_sensible=3.0, cop_latent=3.0,
                 rom=None, h_inner_min=5.0, h_inner_max=20.0, h_inner_beta=1.0,
                 h_outer_base=5.7, h_outer_slope=3.8,
                 A_inner=None, rom_theta=0.55,
                 door_use_stochastic=False,
                 door_open_frac_per_event=0.05, door_open_frac_std=0.02, door_open_frac_cap=0.25,
                 door_ACH_during_open=20.0, door_ACH_std=5.0, door_ACH_cap=50.0,
                 carcass_mass_avg_kg=350.0,
                 plot=False):
        if T_out_series is None:
            t = np.arange(hours)
            T_out_series = 10.0 + 10.0 * np.sin(2 * np.pi * (t - 200) / 8760.0)
        if RH_out_series is None:
            RH_out_series = np.clip(0.6 + 0.2 * np.sin(2 * np.pi * np.arange(hours) / 8760.0 + 1.0), 0.3, 0.95)
        if airflow_series is None:
            airflow_series = np.full(hours, 5000.0)
        if wind_series is None:
            wind_series = np.full(hours, 2.0)
        if A_inner is None:
            A_inner = self.A_inner_total
        T_in = np.zeros(hours)
        RH_in = np.zeros(hours)
        airflow_mech = np.zeros(hours)
        airflow_total = np.zeros(hours)
        h_inner_series = np.zeros(hours)
        h_outer_series = np.zeros(hours)
        Q_sensible_kWh = np.zeros(hours)
        Q_latent_kWh = np.zeros(hours)
        E_el_cool_kWh = np.zeros(hours)
        E_el_latent_kWh = np.zeros(hours)
        E_el_total_kWh = np.zeros(hours)
        beef_kWh = np.zeros(hours)
        n_in_arr = np.zeros(hours, dtype=int)
        n_out_arr = np.zeros(hours, dtype=int)
        n_active_arr = np.zeros(hours, dtype=int)
        door_events = np.zeros(hours, dtype=int)
        door_frac_sum = np.zeros(hours)
        door_frac_mean = np.zeros(hours)
        door_ach_mean = np.zeros(hours)
        infil_m3 = np.zeros(hours)
        mass_in_kg = np.zeros(hours)
        mass_out_kg = np.zeros(hours)
        T_in[0] = self.T_set
        RH_in[0] = self.RH_set
        active_tasks = []
        dt = 3600.0
        AF_min, AF_max = self.airflow_bounds
        for h in range(hours):
            Tout = float(T_out_series[h])
            RHout = float(RH_out_series[h])
            v = float(wind_series[h])
            n_in = int(np.random.poisson(beef_mean_in))
            n_out = int(np.random.poisson(beef_mean_out))
            n_in_arr[h], n_out_arr[h] = n_in, n_out
            events = n_in + n_out
            door_events[h] = events
            AF_mech = float(np.clip(airflow_series[h], *self.airflow_bounds))
            if door_use_stochastic and events > 0:
                AF_infil = 0.0
                f_sum = 0.0
                ach_sum = 0.0
                for _ in range(events):
                    f = max(0.0, np.random.normal(door_open_frac_per_event, door_open_frac_std))
                    f = float(min(f, door_open_frac_cap))
                    ach = max(0.0, np.random.normal(door_ACH_during_open, door_ACH_std))
                    ach = float(min(ach, door_ACH_cap))
                    AF_infil += f * ach * self.V
                    f_sum += f
                    ach_sum += ach
                door_frac_sum[h] = f_sum
                door_frac_mean[h] = f_sum / events if events > 0 else 0.0
                door_ach_mean[h] = ach_sum / events if events > 0 else 0.0
            else:
                AF_infil = events * door_open_frac_per_event * door_ACH_during_open * self.V
                door_frac_sum[h] = events * door_open_frac_per_event
                door_frac_mean[h] = door_open_frac_per_event if events > 0 else 0.0
                door_ach_mean[h] = door_ACH_during_open if events > 0 else 0.0
            AF_total = AF_mech + AF_infil
            airflow_mech[h] = AF_mech
            airflow_total[h] = AF_total
            infil_m3[h] = max(AF_total - AF_mech, 0.0)
            h_outer_curr = float(h_outer_base + h_outer_slope * v)
            u = 0.0 if AF_max <= AF_min else (AF_total - AF_min) / (AF_max - AF_min)
            u = float(np.clip(u, 0.0, 1.0))
            h_inner_curr = float(h_inner_min + (h_inner_max - h_inner_min) * (u ** h_inner_beta))
            h_inner_series[h] = h_inner_curr
            h_outer_series[h] = h_outer_curr
            for _ in range(n_in):
                if beef_use_weight:
                    wkg = max(50.0, np.random.normal(beef_weight_mu, beef_weight_sigma))
                    E_car = wkg * beef_EkWh_per_kg
                    mass_in_kg[h] += wkg
                else:
                    E_car = np.random.uniform(*beef_E_kWh_range)
                    mass_in_kg[h] += carcass_mass_avg_kg
                active_tasks.append([E_car, beef_chill_hours])
            for _ in range(min(n_out, len(active_tasks))):
                active_tasks.pop(0)
                mass_out_kg[h] += (carcass_mass_avg_kg if not beef_use_weight else beef_weight_mu)
            task_load = 0.0
            new_tasks = []
            for E_rem, hrs_left in active_tasks:
                share = E_rem / hrs_left
                task_load += share
                if hrs_left > 1:
                    new_tasks.append([E_rem - share, hrs_left - 1])
            active_tasks = new_tasks
            beef_kWh[h] = task_load
            n_active_arr[h] = len(active_tasks)
            Tin = float(T_in[h - 1] if h > 0 else T_in[0])
            win = self.RH_to_w(Tin, float(RH_in[h - 1] if h > 0 else RH_in[0]))
            wout = self.RH_to_w(Tout, RHout)
            m_dot = (AF_total / 3600.0) * self.rho
            Q_vent_W = m_dot * self.cp * (Tout - Tin)
            if rom is not None:
                Tin_K = Tin + 273.15
                Tout_K = Tout + 273.15
                rom.step_theta(dt=3600.0, theta=rom_theta,
                               Tin_K_old=Tin_K, Tout_K_old=Tout_K,
                               Tin_K_new=Tin_K, Tout_K_new=Tout_K,
                               h_inner_curr=h_inner_curr, h_outer_curr=h_outer_curr,
                               h_inner_base=h_inner_min, h_outer_base=h_outer_base)
                T_surf_C = rom.surface_T() - 273.15
                Q_cond_W = h_inner_curr * A_inner * (T_surf_C - Tin)
            else:
                Q_cond_W = 0.0
            Q_beef_W = (task_load * 3.6e6) / 3600.0
            Tin_star = Tin + (3600.0 / self.C_air) * (Q_cond_W + Q_vent_W + Q_beef_W)
            T_target = float(np.clip(self.T_set, *self.T_bounds))
            if Tin_star > T_target:
                E_cool_J = (Tin_star - T_target) * self.C_air
                Q_sensible_kWh[h] = E_cool_J / 3.6e6
                Tin_next = T_target
            else:
                Q_sensible_kWh[h] = 0.0
                Tin_next = Tin_star
            win_star = win + (3600.0 * m_dot / (self.rho * self.V)) * (wout - win)
            w_set = self.RH_to_w(Tin_next, self.RH_set)
            if win_star > w_set:
                dm_remove = (win_star - w_set) * (self.rho * self.V)
                Q_latent_kWh[h] = (dm_remove * self.h_fg) / 3.6e6
                w_next = w_set
            else:
                Q_latent_kWh[h] = 0.0
                w_next = win_star
            RH_next = self.w_to_RH(Tin_next, w_next)
            RH_next = float(np.clip(RH_next, *self.RH_bounds))
            E_el_cool_kWh[h] = Q_sensible_kWh[h] / max(cop_sensible, 1e-6)
            E_el_latent_kWh[h] = Q_latent_kWh[h] / max(cop_latent, 1e-6)
            E_el_total_kWh[h] = E_el_cool_kWh[h] + E_el_latent_kWh[h]
            T_in[h] = Tin_next
            RH_in[h] = RH_next
        return dict(T_in=T_in, RH_in=RH_in,
                    airflow_mech=airflow_mech, airflow_total=airflow_total,
                    h_inner=h_inner_series, h_outer=h_outer_series,
                    Q_sensible_kWh=Q_sensible_kWh, Q_latent_kWh=Q_latent_kWh,
                    E_el_cool_kWh=E_el_cool_kWh, E_el_latent_kWh=E_el_latent_kWh, E_el_total_kWh=E_el_total_kWh,
                    beef_kWh=beef_kWh, n_in=n_in_arr, n_out=n_out_arr, n_active=n_active_arr,
                    door_events=door_events, door_frac_sum=door_frac_sum, door_frac_mean=door_frac_mean, door_ach_mean=door_ach_mean,
                    infil_m3=infil_m3, mass_in_kg=mass_in_kg, mass_out_kg=mass_out_kg)

# ==========================================
# ПОКАЗАТЕЛИ И СОХРАНЕНИЕ РЕЗУЛЬТАТОВ
# ==========================================

def compute_kpis(res, args, zone, tariff_eur_per_kWh=0.0, grid_co2_kg_per_kWh=0.0, carcass_mass_avg_kg=350.0):
    T = res['T_in']
    RH = res['RH_in']
    Qs = res['Q_sensible_kWh']
    Ql = res['Q_latent_kWh']
    E_c = res['E_el_cool_kWh']
    E_l = res['E_el_latent_kWh']
    E_tot = res['E_el_total_kWh']
    AF_mech = res['airflow_mech']
    AF_total = res['airflow_total']
    h_in = res['h_inner']
    h_out = res['h_outer']
    beef = res['beef_kWh']
    n_in = res['n_in']
    n_out = res['n_out']
    n_act = res['n_active']
    infil_m3 = res.get('infil_m3', np.zeros_like(T))
    door_events = res.get('door_events', np.zeros_like(n_in))
    door_frac_sum = res.get('door_frac_sum', np.zeros_like(T))
    door_frac_mean = res.get('door_frac_mean', np.zeros_like(T))
    door_ach_mean = res.get('door_ach_mean', np.zeros_like(T))
    mass_in_kg = res.get('mass_in_kg', np.zeros_like(T))
    mass_out_kg = res.get('mass_out_kg', np.zeros_like(T))
    hours = len(T)
    T_low, T_high = zone.T_bounds
    RH_low, RH_high = zone.RH_bounds
    in_T = ((T >= T_low) & (T <= T_high)).astype(int)
    in_RH = ((RH >= RH_low) & (RH <= RH_high)).astype(int)
    T_in_pct = 100.0 * float(np.sum(in_T)) / hours
    RH_in_pct = 100.0 * float(np.sum(in_RH)) / hours
    T_viol = int(np.sum(1 - in_T))
    RH_viol = int(np.sum(1 - in_RH))
    kwh_sens = float(np.sum(Qs))
    kwh_lat = float(np.sum(Ql))
    kwh_el = float(np.sum(E_tot))
    kwh_el_c = float(np.sum(E_c))
    kwh_el_l = float(np.sum(E_l))
    beef_kwh = float(np.sum(beef))
    p_peak_kW = float(np.max(E_tot))
    cop_eff_sens = (kwh_sens / kwh_el_c) if kwh_el_c > 1e-9 else None
    cop_eff_lat = (kwh_lat / kwh_el_l) if kwh_el_l > 1e-9 else None
    carc_in = int(np.sum(n_in))
    carc_out = int(np.sum(n_out))
    carc_throughput = int(min(carc_in, carc_out))
    total_mass_through_kg = float(np.sum(mass_out_kg) if np.sum(mass_out_kg) > 0 else carc_throughput * carcass_mass_avg_kg)
    total_mass_through_t = total_mass_through_kg / 1000.0
    kwh_per_carc = (kwh_el / carc_throughput) if carc_throughput > 0 else None
    kwh_per_ton = (kwh_el / total_mass_through_t) if total_mass_through_t > 1e-9 else None
    infil_total_m3 = float(np.sum(np.maximum(AF_total - AF_mech, 0.0)))
    infil_total_m3_alt = float(np.sum(infil_m3))
    h_in_p5, h_in_p50, h_in_p95 = pct(h_in, 5), pct(h_in, 50), pct(h_in, 95)
    h_out_p5, h_out_p50, h_out_p95 = pct(h_out, 5), pct(h_out, 50), pct(h_out, 95)
    total_events = int(np.sum(door_events))
    total_open_hours = float(np.sum(door_frac_sum))
    mean_open_min = (60.0 * total_open_hours / total_events) if total_events > 0 else 0.0
    mean_ach_when_open = float(np.mean(door_ach_mean[door_events > 0])) if np.any(door_events > 0) else 0.0
    cost_eur = kwh_el * float(tariff_eur_per_kWh)
    co2_kg = kwh_el * float(grid_co2_kg_per_kWh)
    V = zone.V
    kwh_el_per_m3_per_year = (kwh_el * (8760.0 / hours) / V) if (V > 0 and hours > 0) else None
    kpi = dict(
        hours=hours,
        energy_sensible_kWh=kwh_sens,
        energy_latent_kWh=kwh_lat,
        energy_total_el_kWh=kwh_el,
        energy_el_sensible_kWh=kwh_el_c,
        energy_el_latent_kWh=kwh_el_l,
        beef_chilling_kWh=beef_kwh,
        peak_power_kW=p_peak_kW,
        cop_eff_sensible=cop_eff_sens,
        cop_eff_latent=cop_eff_lat,
        carcasses_in=carc_in,
        carcasses_out=carc_out,
        carcasses_throughput=carc_throughput,
        mass_throughput_tonnes=total_mass_through_t,
        kWh_el_per_carcass=kwh_per_carc,
        kWh_el_per_tonne=kwh_per_ton,
        infil_total_m3=infil_total_m3,
        infil_total_m3_alt=infil_total_m3_alt,
        T_violations=T_viol,
        RH_violations=RH_viol,
        T_in_bounds_pct=T_in_pct,
        RH_in_bounds_pct=RH_in_pct,
        T_bounds=list(zone.T_bounds),
        RH_bounds=list(zone.RH_bounds),
        h_inner_p5=h_in_p5, h_inner_p50=h_in_p50, h_inner_p95=h_in_p95,
        h_outer_p5=h_out_p5, h_outer_p50=h_out_p50, h_outer_p95=h_out_p95,
        door_total_events=total_events,
        door_total_open_hours=total_open_hours,
        door_mean_open_minutes=mean_open_min,
        door_mean_ach_when_open=mean_ach_when_open,
        energy_intensity_kWh_el_per_m3_year=kwh_el_per_m3_per_year,
        tariff_eur_per_kWh=float(tariff_eur_per_kWh),
        grid_co2_kg_per_kWh=float(grid_co2_kg_per_kWh),
        energy_cost_eur=cost_eur,
        energy_co2_kg=co2_kg
    )
    return kpi

def prepare_rom_for_zone(args):
    d_list = parse_csv_floats(args.layers_thick_m)
    k_list = parse_csv_floats(args.layers_k_WmK)
    rho_list = parse_csv_floats(args.layers_rho_kgm3)
    cp_list = parse_csv_floats(args.layers_cp_JkgK)
    Lx_eff, k_eff, rho_eff, cp_eff = effective_wall_from_layers(d_list, k_list, rho_list, cp_list)
    Nx = args.Nx
    r = args.r
    theta = args.theta
    hL_base = args.h_inner_base
    hR_base = args.h_outer_base
    alpha_snap, dx_snap, L_snap, S_left_snap, S_right_snap = build_1d_with_h_wrapper(Nx, Lx_eff, k_eff, rho_eff, cp_eff, hL_base, hR_base)
    times = np.linspace(0.0, 6 * 3600.0, 300)
    T0 = np.full(Nx, 273.15 + 2.0)
    TinfL_fn = lambda t: 273.15 + 2.0 if t < 1800 else 273.15 - 1.0
    TinfR_fn = lambda t: 273.15 + 25.0
    snaps = theta_integrate_1d(T0, times, alpha_snap, L_snap, S_left_snap, S_right_snap, TinfL_fn, TinfR_fn, theta=max(theta, 0.55))
    mu, Phi, _ = compute_pod(snaps, r=r)
    alpha, dx, L0, L_L, L_R, S_L, S_R = build_1d_decomposed(Nx, Lx_eff, k_eff, rho_eff, cp_eff)
    rom_parts = build_rom_decomposition(alpha, L0, L_L, L_R, S_L, S_R, Phi, mu)
    A_base = Phi.T @ (alpha_snap * L_snap) @ Phi
    B_base = [Phi.T @ (alpha_snap * S_left_snap),
              Phi.T @ (alpha_snap * S_right_snap)]
    c_base = Phi.T @ (alpha_snap * (L_snap @ mu))
    A_inner = 2 * (args.room_L * args.room_W + args.room_L * args.room_H + args.room_W * args.room_H)
    rom = ConductionROM1D(Phi=Phi, mu=mu,
                          A_base=A_base, B_base=B_base, c_base=c_base,
                          A0=rom_parts["A0"], A_L=rom_parts["A_L"], A_R=rom_parts["A_R"],
                          B_L_unit=rom_parts["B_L_unit"], B_R_unit=rom_parts["B_R_unit"],
                          c0=rom_parts["c0"], c_L_unit=rom_parts["c_L_unit"], c_R_unit=rom_parts["c_R_unit"],
                          idx_surface=0, A_inner=A_inner, dx=dx, k_eff=k_eff)
    return rom, A_inner

def save_outputs(res, args, zone, kpi):
    os.makedirs(args.out_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{args.tag}_{timestamp}"
    if args.save_json:
        summary = {"tag": args.tag, "timestamp": timestamp, "hours": args.hours, "kpi": kpi}
        with open(os.path.join(args.out_dir, base + "_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    if args.save_csv:
        csv_path = os.path.join(args.out_dir, base + "_timeseries.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["hour", "T_in_C", "RH_in", "airflow_mech_m3ph", "airflow_total_m3ph",
                        "h_inner_Wm2K", "h_outer_Wm2K",
                        "Q_sensible_kWh", "Q_latent_kWh",
                        "E_el_cool_kWh", "E_el_latent_kWh", "E_el_total_kWh",
                        "beef_kWh", "n_in", "n_out", "n_active",
                        "door_events", "door_open_frac_sum_h", "door_open_frac_mean_h", "door_ach_mean",
                        "infiltration_m3", "mass_in_kg", "mass_out_kg"])
            H = args.hours
            for h in range(H):
                w.writerow([h,
                            float(res["T_in"][h]), float(res["RH_in"][h]),
                            float(res["airflow_mech"][h]), float(res["airflow_total"][h]),
                            float(res["h_inner"][h]), float(res["h_outer"][h]),
                            float(res["Q_sensible_kWh"][h]), float(res["Q_latent_kWh"][h]),
                            float(res["E_el_cool_kWh"][h]), float(res["E_el_latent_kWh"][h]), float(res["E_el_total_kWh"][h]),
                            float(res["beef_kWh"][h]), int(res["n_in"][h]), int(res["n_out"][h]), int(res["n_active"][h]),
                            int(res.get("door_events", np.zeros(H))[h]),
                            float(res.get("door_frac_sum", np.zeros(H))[h]),
                            float(res.get("door_frac_mean", np.zeros(H))[h]),
                            float(res.get("door_ach_mean", np.zeros(H))[h]),
                            float(res.get("infil_m3", np.zeros(H))[h]),
                            float(res.get("mass_in_kg", np.zeros(H))[h]),
                            float(res.get("mass_out_kg", np.zeros(H))[h])])
        print(f"Saved CSV: {csv_path}")
    if args.dump_config:
        cfg = vars(args).copy()
        if isinstance(cfg.get("wind_series", ""), str) and len(cfg["wind_series"]) > 5000:
            cfg["wind_series"] = cfg["wind_series"][:5000] + "...(truncated)"
        with open(os.path.join(args.out_dir, base + "_config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

# ==========================================
# ИНТЕГРАЦИЯ LLM (llm_analyze_scenarios)
# ==========================================

def scan_csv_stats(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            n = 0
            T_min, T_max = +1e9, -1e9
            RH_min, RH_max = +1e9, -1e9
            sum_in = 0; sum_out = 0; sum_door = 0
            sum_Eel = 0.0
            for row in r:
                n += 1
                try:
                    T = float(row.get(CSV_COLS["T"], "nan"))
                    RH = float(row.get(CSV_COLS["RH"], "nan"))
                    if not math.isnan(T):
                        T_min = min(T_min, T); T_max = max(T_max, T)
                    if not math.isnan(RH):
                        RH_min = min(RH_min, RH); RH_max = max(RH_max, RH)
                except Exception:
                    pass
                try: sum_in += int(float(row.get(CSV_COLS["n_in"], "0")))
                except Exception: pass
                try: sum_out += int(float(row.get(CSV_COLS["n_out"], "0")))
                except Exception: pass
                try: sum_door += int(float(row.get(CSV_COLS["door_ev"], "0")))
                except Exception: pass
                try: sum_Eel += float(row.get(CSV_COLS["E_el_tot"], "0"))
                except Exception: pass
            if n == 0: return None
            return dict(n_rows=n, T_min=T_min, T_max=T_max, RH_min=RH_min, RH_max=RH_max,
                        sum_in=sum_in, sum_out=sum_out, sum_door=sum_door, sum_Eel=sum_Eel)
    except Exception:
        return None

def load_scenarios(input_dir: str) -> List[Dict[str, Any]]:
    scenarios = []
    for spath in glob.glob(os.path.join(input_dir, "*_summary.json")):
        base = os.path.basename(spath).replace("_summary.json", "")
        cfg_path = os.path.join(input_dir, base + "_config.json")
        csv_path = os.path.join(input_dir, base + "_timeseries.csv")
        summ = read_json(spath)
        cfg = read_json(cfg_path) if os.path.isfile(cfg_path) else {}
        csv_stats = scan_csv_stats(csv_path) if os.path.isfile(csv_path) else None
        tag = summ.get("tag", base)
        kpi = summ.get("kpi", {})
        scenarios.append(dict(tag=tag, summary=summ, kpi=kpi, config=cfg, csv_stats=csv_stats,
                              files=dict(summary=spath, config=cfg_path if os.path.isfile(cfg_path) else None,
                                         csv=csv_path if os.path.isfile(csv_path) else None)))
    scenarios.sort(key=lambda x: x["tag"])
    return scenarios

def qc_checks(one: Dict[str, Any]) -> Dict[str, Any]:
    k = one["kpi"]
    cfg = one.get("config", {})
    csvs = one.get("csv_stats")
    issues = []
    warns = []

    req_keys = ["energy_total_el_kWh", "energy_el_sensible_kWh", "energy_el_latent_kWh",
                "energy_sensible_kWh", "energy_latent_kWh", "T_in_bounds_pct", "RH_in_bounds_pct"]
    missing = [kk for kk in req_keys if kk not in k]
    if missing: issues.append(f"Missing KPI fields: {missing}")

    for kk in ["energy_total_el_kWh", "energy_el_sensible_kWh", "energy_el_latent_kWh",
               "energy_sensible_kWh", "energy_latent_kWh", "beef_chilling_kWh", "peak_power_kW"]:
        v = k.get(kk, 0.0)
        if v is None: continue
        if isinstance(v, (int, float)) and v < -1e-6:
            issues.append(f"Negative value {kk}={v}")

    Etot = k.get("energy_total_el_kWh", 0.0)
    Ec = k.get("energy_el_sensible_kWh", 0.0)
    El = k.get("energy_el_latent_kWh", 0.0)
    if isinstance(Etot, (int, float)) and isinstance(Ec, (int, float)) and isinstance(El, (int, float)):
        if abs(Etot - (Ec + El)) > max(1e-2, 0.01 * max(Etot, 1.0)):
            warns.append(f"Etot != Ec+El (Etot={fmt(Etot)}, Ec+El={fmt(Ec+El)})")

    for name in ["cop_eff_sensible", "cop_eff_latent"]:
        v = k.get(name, None)
        if v is not None and (v < 1.0 or v > 7.0):
            warns.append(f"{name} unusual ({fmt(v)})")

    for name in ["T_in_bounds_pct", "RH_in_bounds_pct"]:
        v = k.get(name, 0.0)
        if v < 70.0:
            warns.append(f"Low {name}={fmt(v)}% (comfort violations likely)")

    infil_a = k.get("infil_total_m3", 0.0)
    infil_b = k.get("infil_total_m3_alt", 0.0)
    if isinstance(infil_a, (int, float)) and isinstance(infil_b, (int, float)):
        if max(infil_a, infil_b) > 0 and abs(infil_a - infil_b) / max(infil_a, infil_b) > 0.05:
            warns.append(f"infil_total_m3 mismatch (A={fmt(infil_a)}, B={fmt(infil_b)})")

    for name in ["h_inner_p5", "h_inner_p50", "h_inner_p95", "h_outer_p5", "h_outer_p50", "h_outer_p95"]:
        v = k.get(name, None)
        if v is not None and v <= 0:
            issues.append(f"{name} <= 0 (={v})")

    mean_open_min = k.get("door_mean_open_minutes", 0.0)
    if mean_open_min > 15.0:
        warns.append(f"Door mean opening unusually long ({fmt(mean_open_min)} min)")

    e_int = k.get("energy_intensity_kWh_el_per_m3_year", None)
    if e_int is not None and (e_int < 1.0 or e_int > 500.0):
        warns.append(f"Energy intensity out of soft bounds ({fmt(e_int)} kWh_el/m3/yr)")

    hours = k.get("hours", None)
    if csvs is not None:
        if hours is not None and csvs["n_rows"] != int(hours):
            warns.append(f"CSV rows ({csvs['n_rows']}) != hours ({hours})")
        T_min, T_max = csvs["T_min"], csvs["T_max"]
        RH_min, RH_max = csvs["RH_min"], csvs["RH_max"]
        if T_min < -35 or T_max > 15:
            warns.append(f"T extrema unusual (min={fmt(T_min)}, max={fmt(T_max)})")
        if RH_min < 0.0 or RH_max > 1.1:
            warns.append(f"RH extrema unusual (min={fmt(RH_min)}, max={fmt(RH_max)})")
        carc_in = k.get("carcasses_in", None)
        carc_out = k.get("carcasses_out", None)
        if carc_in is not None and abs(csvs["sum_in"] - carc_in) > max(1, 0.01 * max(carc_in, 1)):
            warns.append(f"Sum(n_in in CSV)={csvs['sum_in']} != KPI carcasses_in={carc_in}")
        if carc_out is not None and abs(csvs["sum_out"] - carc_out) > max(1, 0.01 * max(carc_out, 1)):
            warns.append(f"Sum(n_out in CSV)={csvs['sum_out']} != KPI carcasses_out={carc_out}")
        door_events_kpi = k.get("door_total_events", None)
        if door_events_kpi is not None and abs(csvs["sum_door"] - door_events_kpi) > max(1, 0.01 * max(door_events_kpi, 1)):
            warns.append(f"Sum(door_events CSV)={csvs['sum_door']} != KPI door_total_events={door_events_kpi}")

    beef_kwh_th = k.get("beef_chilling_kWh", None)
    carc_throughput = k.get("carcasses_throughput", None)
    if beef_kwh_th is not None and carc_throughput and carc_throughput > 0:
        per_carc_th = beef_kwh_th / carc_throughput
        if per_carc_th < 3.0 or per_carc_th > 20.0:
            warns.append(f"Beef thermal per carcass unusual ({fmt(per_carc_th)} kWh_th/car)")

    kwh_el = k.get("energy_total_el_kWh", None)
    mass_t = k.get("mass_throughput_tonnes", None)
    if kwh_el is not None and mass_t and mass_t > 0 and hours:
        per_ton_year = (kwh_el / mass_t) * (8760.0 / float(hours))
        if per_ton_year < 50.0 or per_ton_year > 1000.0:
            warns.append(f"kWh_el per tonne (annualized) unusual ({fmt(per_ton_year)} kWh/t-yr)")

    status = "green" if not issues and not warns else ("amber" if not issues else "red")
    return dict(status=status, issues=issues, warns=warns)

def build_prompt(scenarios: List[Dict[str, Any]], qc: Dict[str, Any]) -> str:
    lines = []
    lines.append("You are an energy optimization analyst for industrial cold storage.")
    lines.append("Task 1 — Plausibility & artifacts: check physics, units, balances, CSV-vs-KPI consistency (events, hours), T/RH extrema sanity, soft corridors: [3–20] kWh_th per carcass; [50–1000] kWh_el/t-yr.")
    lines.append("Task 2 — Comparative analysis: rank scenarios by energy_total_el_kWh, energy_cost_eur, energy_co2_kg, comfort (T_violations, RH_violations, T/RH_in_bounds_pct), and logistics (door/infiltration).")
    lines.append("Task 3 — Recommendations: for each scenario give 3–5 actionable steps and 1 follow-up scenario with CLI flags.")
    lines.append("Return clear Markdown with sections per scenario + a global ranking table.")
    lines.append("")
    lines.append("=== QUICK QC SUMMARY ===")
    for tag, q in qc.items():
        lines.append(f"- {tag}: status={q['status']}; issues={len(q['issues'])}; warnings={len(q['warns'])}")
    lines.append("")
    lines.append("=== SCENARIO KPI SNAPSHOTS ===")
    for s in scenarios:
        k = s["kpi"]
        tag = s["tag"]
        lines.append(f"\n# {tag}")
        for key in ["hours", "energy_total_el_kWh", "energy_cost_eur", "energy_co2_kg",
                    "energy_sensible_kWh", "energy_latent_kWh",
                    "kWh_el_per_tonne", "kWh_el_per_carcass",
                    "beef_chilling_kWh", "carcasses_throughput",
                    "peak_power_kW",
                    "T_in_bounds_pct", "RH_in_bounds_pct", "T_violations", "RH_violations",
                    "infil_total_m3", "door_total_events", "door_total_open_hours",
                    "h_inner_p50", "h_outer_p50", "energy_intensity_kWh_el_per_m3_year"]:
            if key in k:
                lines.append(f"- {key}: {k.get(key)}")
    lines.append("\n(You may infer relative comparisons if a KPI is missing.)")
    return "\n".join(lines)

import requests

def call_gemini(model: str, api_key: str, prompt: str) -> str:
    """
    Выполняет прямой HTTP-запрос к API Gemini для анализа данных.
    Использует системную инструкцию для задания роли инженерного аналитика.
    """
    api_url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "systemInstruction": {
            "parts": [{"text": "You are a precise, skeptical energy engineer."}]
        },
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2200
        }
    }
    
    try:
        response = requests.post(api_url, headers=headers, json=payload)
        response.raise_for_status() # Вызывает исключение для HTTP ошибок (например, 400, 404, 401)
        data = response.json()
        
        # Извлечение текста из структуры ответа Gemini
        return data['candidates'][0]['content']['parts'][0]['text']
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        if response is not None and hasattr(response, 'text'):
            error_msg += f" | Детали сервера: {response.text}"
        return f"_Ошибка вызова Gemini API: {error_msg}_"
    

def write_report(path: str, scenarios: List[Dict[str, Any]], qc_map: Dict[str, Any], llm_text: str = None):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Cold Storage Scenarios — QC & LLM Analysis\n\nGenerated: {ts}\n\n")
        f.write("## Quick QC status\n")
        for tag, q in qc_map.items():
            f.write(f"- **{tag}** — status: **{q['status']}**")
            if q["issues"]: f.write(f"; issues: {len(q['issues'])}")
            if q["warns"]: f.write(f"; warnings: {len(q['warns'])}")
            f.write("\n")
        f.write("\n---\n\n")
        f.write("## QC Details\n")
        for tag, q in qc_map.items():
            f.write(f"### {tag}\n")
            if q["issues"]:
                f.write("**Issues:**\n")
                for it in q["issues"]: f.write(f"- {it}\n")
            if q["warns"]:
                f.write("\n**Warnings:**\n")
                for it in q["warns"]: f.write(f"- {it}\n")
            if not q["issues"] and not q["warns"]:
                f.write("_No issues or warnings._\n")
            f.write("\n")
        if llm_text:
            f.write("\n---\n\n")
            f.write("## LLM Comparative Analysis & Recommendations\n\n")
            f.write(llm_text)
            f.write("\n")
    return path

# ==========================================
# ИСПОЛНИТЕЛЬНЫЕ ФУНКЦИИ (CLI)
# ==========================================

def run_zone(args):
    rom = None
    A_inner = None
    if args.rom_enable:
        rom, A_inner = prepare_rom_for_zone(args)
    wind_series = parse_csv_floats(args.wind_series)
    if wind_series is not None:
        wind_series = np.array(wind_series, dtype=float)
        if wind_series.size < args.hours:
            wind_series = np.pad(wind_series, (0, args.hours - wind_series.size), mode='edge')
    zone = ColdRoomZone(room_L=args.room_L, room_W=args.room_W, room_H=args.room_H,
                        T_bounds=(-0.5, 4.0), RH_bounds=(0.75, 0.90),
                        T_set=2.0, RH_set=0.80)
    res = zone.simulate(hours=args.hours,
                        wind_series=wind_series,
                        beef_mean_in=args.beef_mean_in, beef_mean_out=args.beef_mean_out,
                        beef_E_kWh_range=(args.beef_E_min, args.beef_E_max),
                        beef_use_weight=args.beef_use_weight,
                        beef_weight_mu=args.beef_weight_mu, beef_weight_sigma=args.beef_weight_sigma,
                        beef_EkWh_per_kg=args.beef_EkWh_per_kg,
                        beef_chill_hours=args.beef_chill_hours,
                        cop_sensible=args.cop_sensible, cop_latent=args.cop_latent,
                        rom=rom,
                        h_inner_min=args.h_inner_min, h_inner_max=args.h_inner_max, h_inner_beta=args.h_inner_beta,
                        h_outer_base=args.h_outer_base, h_outer_slope=args.h_outer_slope,
                        A_inner=A_inner, rom_theta=max(args.theta, 0.55),
                        door_use_stochastic=args.door_use_stochastic,
                        door_open_frac_per_event=args.door_open_frac_per_event,
                        door_open_frac_std=args.door_open_frac_std,
                        door_open_frac_cap=args.door_open_frac_cap,
                        door_ACH_during_open=args.door_ACH_during_open,
                        door_ACH_std=args.door_ACH_std,
                        door_ACH_cap=args.door_ACH_cap,
                        carcass_mass_avg_kg=args.carcass_mass_avg_kg,
                        plot=False)
    sens = res["Q_sensible_kWh"].sum()
    lat = res["Q_latent_kWh"].sum()
    el = res["E_el_total_kWh"].sum()
    beefE = res["beef_kWh"].sum()
    print(f"Summary for {args.hours} h:")
    print(f"  Sensible cooling energy: {sens:.1f} kWh")
    print(f"  Latent (dehumid) energy: {lat:.1f} kWh")
    print(f"  Electric energy (COPs):  {el:.1f} kWh_el")
    print(f"  Beef chilling energy:    {beefE:.1f} kWh")
    print(f"  Total carcasses in:      {res['n_in'].sum()} ; max active: {res['n_active'].max()}")
    kpi = compute_kpis(res, args, zone, args.tariff_eur_per_kWh, args.grid_co2_kg_per_kWh, args.carcass_mass_avg_kg)
    save_outputs(res, args, zone, kpi)

def run_conduction(args):
    d_list = parse_csv_floats(args.layers_thick_m)
    k_list = parse_csv_floats(args.layers_k_WmK)
    rho_list = parse_csv_floats(args.layers_rho_kgm3)
    cp_list = parse_csv_floats(args.layers_cp_JkgK)
    Lx_eff, k_eff, rho_eff, cp_eff = effective_wall_from_layers(d_list, k_list, rho_list, cp_list)
    Nx = args.Nx
    hL = args.h_inner_base
    hR = args.h_outer_base
    T_init = args.Tinit
    Tinf_inside_after = args.Tinf_in
    Tinf_outside = args.Tinf_out
    t_step = args.t_step
    T_end = args.Tend
    Nt = args.Nt
    r = args.r
    device = args.device
    theta = args.theta
    alpha_b, dx_b, L_b, S_left_b, S_right_b = build_1d_with_h(Nx, Lx_eff, k_eff, rho_eff, cp_eff, hL, hR)
    times = np.linspace(0.0, T_end, Nt)

    def TinfL_fn(t): return T_init if t < t_step else Tinf_inside_after
    def TinfR_fn(t): return Tinf_outside
    T0 = np.full(Nx, T_init, dtype=np.float64)
    snaps = theta_integrate_1d(T0, times, alpha_b, L_b, S_left_b, S_right_b, TinfL_fn, TinfR_fn, theta=theta)
    mu, Phi, _ = compute_pod(snaps, r=r)
    A_rom = Phi.T @ (alpha_b * L_b) @ Phi
    B_list = [Phi.T @ (alpha_b * S_left_b), Phi.T @ (alpha_b * S_right_b)]
    c_rom = Phi.T @ (alpha_b * (L_b @ mu))
    a0 = Phi.T @ (T0 - mu)

    def TinfL_torch(tt):
        return torch.where(tt.squeeze(-1) < t_step, torch.full_like(tt.squeeze(-1), T_init), torch.full_like(tt.squeeze(-1), Tinf_inside_after))

    def TinfR_torch(tt): return torch.full_like(tt.squeeze(-1), Tinf_outside)
    model = train_pinn_for_rom(A_rom, B_list, c_rom, a0, T_end, device=device, steps=args.train_steps,
                               lr=1e-3, n_colloc=256, Tinf_time_fns=[TinfL_torch, TinfR_torch], verbose=True)
    with torch.no_grad():
        t_grid = to_tensor(times.reshape(-1, 1), device=device)
        a_pred = model(t_grid).cpu().numpy().T
    T_pred = (mu.reshape(-1, 1) + Phi @ a_pred)
    x = np.linspace(0, Lx_eff, Nx)
    plt.figure(figsize=(8, 4))
    plt.plot(x, snaps[:, 0], label='t=0 (FDM)')
    plt.plot(x, snaps[:, -1], label='FDM final')
    plt.plot(x, T_pred[:, -1], '--', label='PINN-ROM final')
    plt.xlabel('x (m)')
    plt.ylabel('T (K)')
    plt.title(f'1-D conduction (θ={theta})')
    plt.legend()
    plt.tight_layout()
    plt.show()

def run_batch_cli(args):
    path = args.batch_file
    if not os.path.isfile(path):
        print(f"Batch file not found: {path}")
        return
    scenarios = None
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        scenarios = data if isinstance(data, list) else data.get("scenarios", [])
    except Exception:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scenarios = data if isinstance(data, list) else data.get("scenarios", [])
        except Exception as e:
            print("Failed to parse batch file as YAML or JSON:", e)
            return
    if not scenarios:
        print("No scenarios found in batch file.")
        return
    print(f"Batch run: {len(scenarios)} scenarios")
    for i, sc in enumerate(scenarios, 1):
        print(f"\n--- Scenario {i}/{len(scenarios)}: {sc.get('tag','no-tag')} ---")
        local = argparse.Namespace(**vars(args))
        for k, v in sc.items():
            setattr(local, k, v)
        if not getattr(local, "save_json", False): local.save_json = True
        if not getattr(local, "save_csv", False): local.save_csv = True
        if not getattr(local, "dump_config", False): local.dump_config = True
        run_zone(local)

def run_llm_cli(args):
    scenarios = load_scenarios(args.input_dir)
    if not scenarios:
        print("No scenarios found in:", args.input_dir)
        return
    qc = {s["tag"]: qc_checks(s) for s in scenarios}
    prompt = build_prompt(scenarios, qc)
    llm_text = None
    if args.use_llm:
        if not args.api_key:
            print("ERROR: provide --api_key or set OPENAI_API_KEY")
            return
        try:
            llm_text = call_gemini(args.model, args.api_key, prompt)
        except Exception as e:
            print("LLM call failed:", e)
            llm_text = f"_LLM call failed: {e}_"
    out_path = os.path.join(args.input_dir, args.report)
    write_report(out_path, scenarios, qc, llm_text)
    print("QC summary:")
    for tag, q in qc.items():
        print(f"- {tag}: status={q['status']} (issues={len(q['issues'])}, warns={len(q['warns'])})")
    print(f"\nReport written: {out_path}")

# ==========================================
# ИСПОЛНИТЕЛЬНЫЕ ФУНКЦИИ (UI GRADIO)
# ==========================================

def _latest_files_by_tag(tag: str):
    files = sorted(glob.glob(os.path.join(OUT_DIR, f"{tag}_*_*.*")), key=os.path.getmtime, reverse=True)
    return files[:10]

def run_single_scenario(
    tag, hours, rom_enable, wind_series, door_use_stochastic,
    beef_mean_in, beef_mean_out,
    cop_sensible, cop_latent,
    tariff, grid_co2,
    h_inner_min, h_inner_max, h_inner_beta,
    h_outer_base, h_outer_slope
):
    args = SimpleNamespace(
        mode='zone',
        layers_thick_m='0.2',
        layers_k_WmK='0.035',
        layers_rho_kgm3='30.0',
        layers_cp_JkgK='1400.0',
        Nx=81,
        h_inner_base=10.0,
        h_outer_base=float(h_outer_base),
        h_outer_slope=float(h_outer_slope),
        Tinit=273.15 + 10.0, Tinf_in=273.15 - 10.0, Tinf_out=273.15 + 20.0,
        t_step=300.0, Tend=3600.0, Nt=400, r=6, train_steps=1500, device='cpu', theta=0.55,
        room_L=10.0, room_W=10.0, room_H=4.0,
        hours=int(hours), rom_enable=bool(rom_enable),
        beef_mean_in=float(beef_mean_in), beef_mean_out=float(beef_mean_out),
        beef_E_min=5.6, beef_E_max=10.0,
        beef_use_weight=False, beef_weight_mu=350.0, beef_weight_sigma=50.0, beef_EkWh_per_kg=(7.8 / 350.0),
        beef_chill_hours=8, carcass_mass_avg_kg=350.0,
        cop_sensible=float(cop_sensible), cop_latent=float(cop_latent),
        wind_series=str(wind_series or ""),
        h_inner_min=float(h_inner_min), h_inner_max=float(h_inner_max), h_inner_beta=float(h_inner_beta),
        door_use_stochastic=bool(door_use_stochastic),
        door_open_frac_per_event=0.05, door_open_frac_std=0.02, door_open_frac_cap=0.25,
        door_ACH_during_open=20.0, door_ACH_std=5.0, door_ACH_cap=50.0,
        tariff_eur_per_kWh=float(tariff), grid_co2_kg_per_kWh=float(grid_co2),
        tag=str(tag or "ui"), out_dir=OUT_DIR, save_csv=True, save_json=True, dump_config=True,
        batch_file=""
    )

    rom = None
    A_inner = None
    if args.rom_enable:
        rom, A_inner = prepare_rom_for_zone(args)

    wind = parse_csv_floats(args.wind_series)
    if wind is None:
        wind = [3.0] * args.hours
    if len(wind) < args.hours:
        wind = wind + [wind[-1]] * (args.hours - len(wind))

    zone = ColdRoomZone(room_L=args.room_L, room_W=args.room_W, room_H=args.room_H,
                        T_bounds=(-0.5, 4.0), RH_bounds=(0.75, 0.90), T_set=2.0, RH_set=0.80)

    res = zone.simulate(hours=args.hours,
                        wind_series=wind,
                        beef_mean_in=args.beef_mean_in, beef_mean_out=args.beef_mean_out,
                        beef_E_kWh_range=(args.beef_E_min, args.beef_E_max),
                        beef_use_weight=args.beef_use_weight,
                        beef_weight_mu=args.beef_weight_mu, beef_weight_sigma=args.beef_weight_sigma,
                        beef_EkWh_per_kg=args.beef_EkWh_per_kg,
                        beef_chill_hours=args.beef_chill_hours,
                        cop_sensible=args.cop_sensible, cop_latent=args.cop_latent,
                        rom=rom,
                        h_inner_min=args.h_inner_min, h_inner_max=args.h_inner_max, h_inner_beta=args.h_inner_beta,
                        h_outer_base=args.h_outer_base, h_outer_slope=args.h_outer_slope,
                        A_inner=A_inner, rom_theta=max(args.theta, 0.55),
                        door_use_stochastic=args.door_use_stochastic,
                        door_open_frac_per_event=args.door_open_frac_per_event,
                        door_open_frac_std=args.door_open_frac_std,
                        door_open_frac_cap=args.door_open_frac_cap,
                        door_ACH_during_open=args.door_ACH_during_open,
                        door_ACH_std=args.door_ACH_std,
                        door_ACH_cap=args.door_ACH_cap,
                        carcass_mass_avg_kg=args.carcass_mass_avg_kg,
                        plot=False)

    kpi = compute_kpis(res, args, zone, args.tariff_eur_per_kWh, args.grid_co2_kg_per_kWh, args.carcass_mass_avg_kg)
    save_outputs(res, args, zone, kpi)
    files = _latest_files_by_tag(args.tag)
    return json.dumps(kpi, ensure_ascii=False, indent=2), files

def run_batch_ui(yaml_file):
    if yaml_file is None:
        return "Upload scenarios.yaml or JSON", []
    dst = os.path.join(OUT_DIR, "scenarios_uploaded.yaml")
    shutil.copyfile(yaml_file, dst)
    args = SimpleNamespace(
        mode='zone',
        layers_thick_m='0.2', layers_k_WmK='0.035', layers_rho_kgm3='30.0', layers_cp_JkgK='1400.0',
        Nx=81, h_inner_base=10.0, h_outer_base=13.3, h_outer_slope=3.8,
        Tinit=283.15, Tinf_in=263.15, Tinf_out=293.15,
        t_step=300.0, Tend=3600.0, Nt=400, r=6, train_steps=1500, device='cpu', theta=0.55,
        room_L=10.0, room_W=10.0, room_H=4.0,
        hours=168, rom_enable=True,
        beef_mean_in=2.0, beef_mean_out=2.0,
        beef_E_min=5.6, beef_E_max=10.0, beef_use_weight=False, beef_weight_mu=350.0, beef_weight_sigma=50.0, beef_EkWh_per_kg=(7.8 / 350.0),
        beef_chill_hours=8, carcass_mass_avg_kg=350.0,
        cop_sensible=3.0, cop_latent=3.0,
        wind_series="3", h_inner_min=5.0, h_inner_max=20.0, h_inner_beta=1.0,
        door_use_stochastic=True, door_open_frac_per_event=0.05, door_open_frac_std=0.02, door_open_frac_cap=0.25,
        door_ACH_during_open=20.0, door_ACH_std=5.0, door_ACH_cap=50.0,
        tariff_eur_per_kWh=0.2, grid_co2_kg_per_kWh=0.35,
        tag="batch", out_dir=OUT_DIR, save_csv=True, save_json=True, dump_config=True,
        batch_file=dst
    )
    run_batch_cli(args)
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*_summary.json")), key=os.path.getmtime, reverse=True)[:30]
    return f"Batch finished. Produced {len(files)} summaries.", files

def run_llm_ui(input_dir, use_llm, model, api_key):
    if not os.path.isdir(input_dir):
        return "Input dir not found", None, []
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "")
    args = SimpleNamespace(input_dir=input_dir, use_llm=bool(use_llm), model=model, api_key=api_key, report="analysis_report.md")
    scenarios = load_scenarios(args.input_dir)
    if not scenarios:
        return "No summaries found in out/ . Run a scenario first.", None, []
    qc = {s["tag"]: qc_checks(s) for s in scenarios}
    prompt = build_prompt(scenarios, qc)
    llm_text = None
    if args.use_llm:
        if not args.api_key:
            return "No API key provided. Set OPENAI_API_KEY in Space Secrets or pass here.", None, []
        llm_text = call_gemini(args.model, args.api_key, prompt)
    out_path = os.path.join(args.input_dir, args.report)
    write_report(out_path, scenarios, qc, llm_text)
    with open(out_path, "r", encoding="utf-8") as f:
        md = f.read()
    files = [out_path]
    return "Analysis complete.", md, files

def launch_ui():
    with gr.Blocks() as demo:
        gr.Markdown("# Cold Storage — PINN-ROM demo (1‑D) • Interactive")
        gr.Markdown("Run scenarios, batch YAML, and LLM analysis.")

        with gr.Tab("Single Scenario"):
            with gr.Row():
                tag = gr.Textbox(label="Scenario tag", value="ui_demo")
                hours = gr.Slider(label="Hours", minimum=24, maximum=8760, step=24, value=168)
                rom_enable = gr.Checkbox(label="Enable ROM", value=True)
                door_stoch = gr.Checkbox(label="Stochastic doors", value=True)
            wind_series = gr.Textbox(label="Wind series (CSV or scalar)", value="3")
            with gr.Accordion("HVAC & economics", open=False):
                with gr.Row():
                    cop_s = gr.Number(label="COP sensible", value=3.0)
                    cop_l = gr.Number(label="COP latent", value=3.0)
                    tariff = gr.Number(label="Tariff €/kWh", value=0.20)
                    co2 = gr.Number(label="Grid kgCO₂/kWh", value=0.35)
            with gr.Accordion("Logistics (carcasses)", open=False):
                with gr.Row():
                    beef_in = gr.Number(label="Mean arrivals per hour", value=2.0)
                    beef_out = gr.Number(label="Mean departures per hour", value=2.0)
            with gr.Accordion("Convection (h)", open=False):
                with gr.Row():
                    h_in_min = gr.Number(label="h_inner_min", value=6.0)
                    h_in_max = gr.Number(label="h_inner_max", value=20.0)
                    h_in_beta = gr.Number(label="h_inner_beta", value=1.2)
                with gr.Row():
                    h_out_base = gr.Number(label="h_outer_base", value=5.7)
                    h_out_slope = gr.Number(label="h_outer_slope", value=3.8)
            run_btn = gr.Button("Run scenario")
            kpi_json = gr.Code(label="KPI JSON")
            files_out = gr.Files(label="Output files")
            run_btn.click(
                run_single_scenario,
                inputs=[tag, hours, rom_enable, wind_series, door_stoch, beef_in, beef_out, cop_s, cop_l, tariff, co2,
                        h_in_min, h_in_max, h_in_beta, h_out_base, h_out_slope],
                outputs=[kpi_json, files_out]
            )

        with gr.Tab("Batch (YAML)"):
            yaml_file = gr.File(label="Upload scenarios.yaml", file_count="single")
            run_batch_btn = gr.Button("Run batch")
            batch_log = gr.Textbox(label="Batch log")
            batch_files = gr.Files(label="Batch output files")
            run_batch_btn.click(run_batch_ui, inputs=[yaml_file], outputs=[batch_log, batch_files])

        with gr.Tab("LLM Analysis"):
            input_dir = gr.Textbox(label="Input dir", value=OUT_DIR)
            # Замените старые строки интерфейса LLM на эти:
            use_llm = gr.Checkbox(label="Использовать Gemini LLM", value=False)
            model = gr.Textbox(label="Модель", value="gemini-2.5-flash")
            api_key = gr.Textbox(label="API ключ Google AI Studio (обязательно)", type="password")
            run_llm_btn = gr.Button("Run analysis")
            llm_log = gr.Textbox(label="Status")
            md_out = gr.Markdown(label="Report (Markdown)")
            files_rep = gr.Files(label="Report files")
            run_llm_btn.click(run_llm_ui, inputs=[input_dir, use_llm, model, api_key], outputs=[llm_log, md_out, files_rep])

    demo.queue().launch()

# ==========================================
# ТОЧКА ВХОДА И УПРАВЛЕНИЕ АРГУМЕНТАМИ
# ==========================================

def main():
    if len(sys.argv) == 1:
        launch_ui()
        return

    p = argparse.ArgumentParser(description='Integrated 1-D Cold Storage & LLM Analyzer')
    
    # Флаги симуляции
    p.add_argument('--mode', type=str, default='zone', choices=['conduction', 'zone'])
    p.add_argument('--layers_thick_m', type=str, default='0.2')
    p.add_argument('--layers_k_WmK', type=str, default='0.035')
    p.add_argument('--layers_rho_kgm3', type=str, default='30.0')
    p.add_argument('--layers_cp_JkgK', type=str, default='1400.0')
    p.add_argument('--Nx', type=int, default=81)
    p.add_argument('--h_inner_base', type=float, default=10.0)
    p.add_argument('--h_outer_base', type=float, default=13.3)
    p.add_argument('--h_outer_slope', type=float, default=3.8)
    p.add_argument('--Tinit', type=float, default=273.15 + 10.0)
    p.add_argument('--Tinf_in', type=float, default=273.15 - 10.0)
    p.add_argument('--Tinf_out', type=float, default=273.15 + 20.0)
    p.add_argument('--t_step', type=float, default=300.0)
    p.add_argument('--Tend', type=float, default=3600.0)
    p.add_argument('--Nt', type=int, default=400)
    p.add_argument('--r', type=int, default=6)
    p.add_argument('--train_steps', type=int, default=1500)
    p.add_argument('--device', type=str, default='cpu')
    p.add_argument('--theta', type=float, default=0.55)
    p.add_argument('--room_L', type=float, default=10.0)
    p.add_argument('--room_W', type=float, default=10.0)
    p.add_argument('--room_H', type=float, default=4.0)
    p.add_argument('--hours', type=int, default=168)
    p.add_argument('--rom_enable', action='store_true')
    p.add_argument('--beef_mean_in', type=float, default=1.0)
    p.add_argument('--beef_mean_out', type=float, default=1.0)
    p.add_argument('--beef_E_min', type=float, default=5.6)
    p.add_argument('--beef_E_max', type=float, default=10.0)
    p.add_argument('--beef_use_weight', action='store_true')
    p.add_argument('--beef_weight_mu', type=float, default=350.0)
    p.add_argument('--beef_weight_sigma', type=float, default=50.0)
    p.add_argument('--beef_EkWh_per_kg', type=float, default=(7.8 / 350.0))
    p.add_argument('--beef_chill_hours', type=int, default=8)
    p.add_argument('--carcass_mass_avg_kg', type=float, default=350.0)
    p.add_argument('--cop_sensible', type=float, default=3.0)
    p.add_argument('--cop_latent', type=float, default=3.0)
    p.add_argument('--wind_series', type=str, default='')
    p.add_argument('--h_inner_min', type=float, default=5.0)
    p.add_argument('--h_inner_max', type=float, default=20.0)
    p.add_argument('--h_inner_beta', type=float, default=1.0)
    p.add_argument('--door_use_stochastic', action='store_true')
    p.add_argument('--door_open_frac_per_event', type=float, default=0.05)
    p.add_argument('--door_open_frac_std', type=float, default=0.02)
    p.add_argument('--door_open_frac_cap', type=float, default=0.25)
    p.add_argument('--door_ACH_during_open', type=float, default=20.0)
    p.add_argument('--door_ACH_std', type=float, default=5.0)
    p.add_argument('--door_ACH_cap', type=float, default=50.0)
    p.add_argument('--tariff_eur_per_kWh', type=float, default=0.0)
    p.add_argument('--grid_co2_kg_per_kWh', type=float, default=0.0)
    p.add_argument('--tag', type=str, default='baseline')
    p.add_argument('--out_dir', type=str, default='out')
    p.add_argument('--save_csv', action='store_true')
    p.add_argument('--save_json', action='store_true')
    p.add_argument('--dump_config', action='store_true')
    p.add_argument('--batch_file', type=str, default='')

    # Флаги LLM
    p.add_argument("--input_dir", type=str, default="", help="Directory with summary.json")
    p.add_argument("--use_llm", action="store_true")
    p.add_argument("--model", type=str, default="gpt-4o-mini")
    p.add_argument("--api_key", type=str, default=os.getenv("OPENAI_API_KEY", ""))
    p.add_argument("--report", type=str, default="analysis_report.md")

    args, _ = p.parse_known_args()

    # Маршрутизация
    if args.input_dir:
        run_llm_cli(args)
    else:
        if args.batch_file:
            run_batch_cli(args)
        elif args.mode == 'conduction':
            run_conduction(args)
        else:
            run_zone(args)

if __name__ == '__main__':
    main()