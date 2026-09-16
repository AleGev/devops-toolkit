#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
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
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
import gradio as gr
import requests

OUT_DIR = "out"
os.makedirs(OUT_DIR, exist_ok=True)

CSV_COLS = {
    "hour": "hour", "T": "T_in_C", "RH": "RH_in",
    "n_in": "n_in", "n_out": "n_out", "door_ev": "door_events",
    "E_el_tot": "E_el_total_kWh",
}

def set_seed(seed=42):
    np.random.seed(seed)
    torch.manual_seed(seed)

def to_tensor(x, device="cpu", dtype=torch.float32):
    return torch.as_tensor(x, device=device, dtype=dtype)

def parse_csv_floats(s):
    if isinstance(s, (list, tuple, np.ndarray)):
        return [float(v) for v in s]
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return [float(x.strip()) for x in s.split(",") if x.strip()]

def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def fmt(v, nd=2):
    if v is None:
        return "null"
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return str(v)

def pct(x, q):
    return float(np.percentile(x, q)) if len(x) else None


# ============================================================
# WALL PHYSICS
# ============================================================

def effective_wall_from_layers(d_list, k_list, rho_list, cp_list):
    d = np.asarray(d_list, dtype=float)
    k = np.asarray(k_list, dtype=float)
    rho = np.asarray(rho_list, dtype=float)
    cp = np.asarray(cp_list, dtype=float)
    if not (len(d) == len(k) == len(rho) == len(cp)):
        raise ValueError("Layer arrays must have equal length.")
    if np.any(d <= 0) or np.any(k <= 0) or np.any(rho <= 0) or np.any(cp <= 0):
        raise ValueError("Wall layer properties must be > 0.")
    L = float(d.sum())
    keff = float(L / np.sum(d / k))
    rho_eff = float(np.sum(rho * d) / L)
    rho_cp_eff = float(np.sum(rho * cp * d) / L)
    cp_eff = float(rho_cp_eff / rho_eff)
    return L, keff, rho_eff, cp_eff

def build_1d_decomposed(Nx, Lx, k, rho, cp):
    if Nx < 3:
        raise ValueError("Nx must be >= 3.")
    dx = Lx / (Nx - 1)
    alpha = k / (rho * cp)
    inv_dx2 = 1.0 / dx**2

    L0 = np.zeros((Nx, Nx), dtype=float)
    for i in range(1, Nx - 1):
        L0[i, i-1] = inv_dx2
        L0[i, i] = -2.0 * inv_dx2
        L0[i, i+1] = inv_dx2

    # Robin boundary spatial operator:
    # d2T/dx2 = 2/dx^2 * (T_1-T_0-Bi(T_0-Tinf))
    L0[0, 0] = -2.0 * inv_dx2
    L0[0, 1] =  2.0 * inv_dx2
    L0[-1, -1] = -2.0 * inv_dx2
    L0[-1, -2] =  2.0 * inv_dx2

    L_L = np.zeros_like(L0)
    L_R = np.zeros_like(L0)
    L_L[0, 0] = -2.0 * inv_dx2
    L_R[-1, -1] = -2.0 * inv_dx2

    S_L = np.zeros(Nx)
    S_R = np.zeros(Nx)
    S_L[0] = 2.0 * inv_dx2
    S_R[-1] = 2.0 * inv_dx2
    return alpha, dx, L0, L_L, L_R, S_L, S_R

def build_1d_with_h(Nx, Lx, k, rho, cp, hL, hR):
    alpha, dx, L0, L_L, L_R, S_L, S_R = build_1d_decomposed(
        Nx, Lx, k, rho, cp
    )
    BiL = hL * dx / k
    BiR = hR * dx / k
    L = L0 + BiL * L_L + BiR * L_R
    S_left = BiL * S_L
    S_right = BiR * S_R
    return alpha, dx, L, S_left, S_right

def theta_step_build(I, L, alpha, dt, theta, Tn, s_old, s_new):
    A = I - theta * dt * alpha * L
    rhs = (
        (I + (1.0 - theta) * dt * alpha * L) @ Tn
        + dt * alpha * (theta * s_new + (1.0 - theta) * s_old)
    )
    return A, rhs

def theta_integrate_1d(
    T0, times, alpha, L, S_left, S_right, TinfL_fn, TinfR_fn, theta=0.55
):
    T = T0.astype(float).copy()
    I = np.eye(len(T0))
    snaps = [T.copy()]
    s_old = S_left * TinfL_fn(times[0]) + S_right * TinfR_fn(times[0])
    for i in range(1, len(times)):
        dt = float(times[i] - times[i-1])
        s_new = S_left * TinfL_fn(times[i]) + S_right * TinfR_fn(times[i])
        A, rhs = theta_step_build(I, L, alpha, dt, theta, T, s_old, s_new)
        T = np.linalg.solve(A, rhs)
        snaps.append(T.copy())
        s_old = s_new
    return np.stack(snaps, axis=1)


# ============================================================
# POD / ROM
# ============================================================

def compute_pod(snapshot_matrix, r):
    mu = np.mean(snapshot_matrix, axis=1, keepdims=True)
    X = snapshot_matrix - mu
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    r_eff = min(int(r), U.shape[1])
    Phi = U[:, :r_eff]
    A_snap = np.diag(S[:r_eff]) @ Vt[:r_eff]
    captured = float(np.sum(S[:r_eff]**2) / max(np.sum(S**2), 1e-30))
    return mu[:, 0], Phi, A_snap, captured

def build_rom_decomposition(alpha, L0, L_L, L_R, S_L, S_R, Phi, mu):
    A0 = Phi.T @ (alpha * L0) @ Phi
    A_L = Phi.T @ (alpha * L_L) @ Phi
    A_R = Phi.T @ (alpha * L_R) @ Phi
    B_L = Phi.T @ (alpha * S_L)
    B_R = Phi.T @ (alpha * S_R)
    c0 = Phi.T @ (alpha * (L0 @ mu))
    cL = Phi.T @ (alpha * (L_L @ mu))
    cR = Phi.T @ (alpha * (L_R @ mu))
    return dict(A0=A0, A_L=A_L, A_R=A_R,
                B_L_unit=B_L, B_R_unit=B_R,
                c0=c0, c_L_unit=cL, c_R_unit=cR)

class ConductionROM1D:
    """
    Galerkin ROM of the 1-D wall with a few numerical safeguards.
    The safeguards enforce the maximum principle after reconstruction:
    wall temperature must remain within the range spanned by its two
    boundary fluid temperatures and the previous wall state, with a small
    numerical tolerance.
    """
    def __init__(
        self, Phi, mu, parts, dx, k_eff, T0_init_val=275.15,
        temp_margin_K=0.25
    ):
        self.Phi = Phi
        self.mu = mu
        self.A0 = parts["A0"]
        self.A_L = parts["A_L"]
        self.A_R = parts["A_R"]
        self.B_L = parts["B_L_unit"]
        self.B_R = parts["B_R_unit"]
        self.c0 = parts["c0"]
        self.cL = parts["c_L_unit"]
        self.cR = parts["c_R_unit"]
        self.dx = float(dx)
        self.k_eff = float(k_eff)
        self.temp_margin_K = float(temp_margin_K)
        T0 = np.full(Phi.shape[0], float(T0_init_val))
        self.a = Phi.T @ (T0 - mu)
        self.last_T = mu + Phi @ self.a

    def surface_T(self):
        return float(self.last_T[0])

    def full_T(self):
        return self.last_T.copy()

    def _project_temperature(self, T, Tin_K, Tout_K):
        lo = min(Tin_K, Tout_K) - self.temp_margin_K
        hi = max(Tin_K, Tout_K) + self.temp_margin_K
        return np.clip(T, lo, hi)

    def step_theta(
        self, dt, theta, Tin_K_old, Tout_K_old,
        Tin_K_new=None, Tout_K_new=None,
        h_inner_curr=10.0, h_outer_curr=10.0
    ):
        if Tin_K_new is None:
            Tin_K_new = Tin_K_old
        if Tout_K_new is None:
            Tout_K_new = Tout_K_old

        BiL = max(h_inner_curr, 0.0) * self.dx / self.k_eff
        BiR = max(h_outer_curr, 0.0) * self.dx / self.k_eff

        A = self.A0 + BiL * self.A_L + BiR * self.A_R
        BL = BiL * self.B_L
        BR = BiR * self.B_R
        c = self.c0 + BiL * self.cL + BiR * self.cR

        I = np.eye(len(self.a))
        old_bc = BL * Tin_K_old + BR * Tout_K_old + c
        new_bc = BL * Tin_K_new + BR * Tout_K_new + c
        rhs = (
            (I + (1.0 - theta) * dt * A) @ self.a
            + dt * ((1.0 - theta) * old_bc + theta * new_bc)
        )
        anew = np.linalg.solve(I - theta * dt * A, rhs)
        Tnew = self.mu + self.Phi @ anew

        # Numerical maximum-principle safeguard.
        Tnew = self._project_temperature(Tnew, Tin_K_new, Tout_K_new)

        # Re-project to the reduced basis so future states remain coherent.
        anew = self.Phi.T @ (Tnew - self.mu)
        self.a = anew
        self.last_T = self.mu + self.Phi @ self.a
        return self.a


# ============================================================
# PINN
# ============================================================

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

def train_pinn_for_rom(
    A_rom, B_list, c_rom, a0, T_end, device="cpu",
    steps=1500, lr=1e-3, n_colloc=256,
    t_step=None, Tinf_time_fns=None, verbose=True,
    target_times=None, target_a=None, data_weight=20.0
):
    """
    PINN for da/dt = A a + B(t).
    Uses normalized time tau=t/T_end and computes per-output derivatives
    correctly. A smooth step is used around a discontinuous BC for training.
    """
    r = len(a0)
    model = MLP(1, r, 64, 3).to(device)
    opt = optim.Adam(model.parameters(), lr=lr)
    A_t = to_tensor(A_rom, device)
    c_t = to_tensor(c_rom, device).view(1, -1)
    a0_t = to_tensor(a0, device).view(1, -1)
    target_t_t = None
    target_a_t = None
    target_scale_t = None
    if target_times is not None and target_a is not None:
        target_t_t = to_tensor(np.asarray(target_times, dtype=float), device).view(-1)
        target_a_t = to_tensor(np.asarray(target_a, dtype=float), device)
        scale = np.std(np.asarray(target_a, dtype=float), axis=0)
        scale = np.where(scale < 1e-6, 1.0, scale)
        target_scale_t = to_tensor(scale, device).view(1, -1)

    if t_step is None:
        t_step = 0.0

    def bc_smooth(tt, before, after):
        width = max(5.0, 0.01 * T_end)
        z = (tt - t_step) / width
        s = torch.sigmoid(z)
        return before + (after - before) * s

    for it in range(steps):
        tau = torch.rand(n_colloc, 1, device=device)
        extra = torch.tensor(
            [[0.0], [0.95*t_step/max(T_end,1.0)],
             [t_step/max(T_end,1.0)],
             [1.05*t_step/max(T_end,1.0)], [1.0]],
            dtype=tau.dtype, device=device
        )
        tau = torch.clamp(torch.cat([tau, extra], 0), 0.0, 1.0)
        tau.requires_grad_(True)
        t_phys = tau * T_end

        N = model(tau)
        a = a0_t + tau * N

        # Correct Jacobian da_j/dtau for each output j.
        derivs = []
        for j in range(r):
            g = torch.autograd.grad(
                a[:, j].sum(), tau, create_graph=True, retain_graph=True
            )[0]
            derivs.append(g[:, 0])
        a_tau = torch.stack(derivs, dim=1)

        if Tinf_time_fns:
            terms = []
            for B, f in zip(B_list, Tinf_time_fns):
                Bt = to_tensor(B, device).view(1, -1)
                fv = f(t_phys)
                if fv.ndim == 1:
                    fv = fv.view(-1, 1)
                terms.append(Bt * fv)
            Bsum = torch.stack(terms, dim=0).sum(dim=0)
        else:
            Bsum = torch.zeros_like(a)

        rhs_tau = T_end * (a @ A_t.T + Bsum + c_t)
        res = a_tau - rhs_tau
        residual_scale = 1.0 + torch.abs(rhs_tau)
        loss_pde = ((res / residual_scale)**2).mean()

        loss_data = torch.zeros((), device=device)
        if target_t_t is not None:
            idx = torch.randint(0, target_t_t.numel(), (tau.shape[0],), device=device)
            tau_data = (target_t_t[idx] / T_end).view(-1, 1)
            N_data = model(tau_data)
            a_data = a0_t + tau_data * N_data
            target_data = target_a_t[idx]
            loss_data = (((a_data - target_data) / target_scale_t)**2).mean()

        loss = loss_pde + float(data_weight) * loss_data

        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 10.0)
        opt.step()

        if verbose and (it + 1) % max(steps//10, 1) == 0:
            print(f"[{it+1}/{steps}] PINN loss={loss.item():.3e} pde={loss_pde.item():.3e} data={loss_data.item():.3e}")

    class PINNWrapper:
        def __init__(self, net, a0_tensor, Tend, dev):
            self.net = net
            self.a0 = a0_tensor
            self.Tend = Tend
            self.device = dev
        def __call__(self, t_phys):
            tt = t_phys if isinstance(t_phys, torch.Tensor) else to_tensor(t_phys, self.device)
            if tt.ndim == 1:
                tt = tt.view(-1, 1)
            tau = tt / self.Tend
            with torch.no_grad():
                return self.a0 + tau * self.net(tau)

    return PINNWrapper(model, a0_t, T_end, device)


# ============================================================
# COLD ROOM
# ============================================================

class ColdRoomZone:
    def __init__(
        self, room_L=10.0, room_W=10.0, room_H=4.0,
        T_set=2.0, RH_set=0.80,
        T_bounds=(-0.5, 4.0), RH_bounds=(0.75, 0.90),
        airflow_bounds=(1000.0, 8000.0),
        rho_air=1.2, cp_air=1006.0, p_atm=101325.0,
        h_fg=2.5e6, seed=123
    ):
        set_seed(seed)
        self.L, self.W, self.H = room_L, room_W, room_H
        self.A_inner_total = 2.0 * (
            room_L*room_W + room_L*room_H + room_W*room_H
        )
        self.V = room_L * room_W * room_H
        self.rho = rho_air
        self.cp = cp_air
        self.p = p_atm
        self.h_fg = h_fg
        self.C_air = rho_air * self.V * cp_air
        self.T_set = float(T_set)
        self.RH_set = float(RH_set)
        self.T_bounds = tuple(T_bounds)
        self.RH_bounds = tuple(RH_bounds)
        self.airflow_bounds = tuple(airflow_bounds)

    @staticmethod
    def sat_vapor_pressure(T_c):
        # Tetens/Magnus form, valid in the cold-room range used here.
        T = float(np.clip(T_c, -50.0, 60.0))
        den = T + 237.3
        return 610.78 * math.exp(17.2694 * T / den)

    def RH_to_w(self, T_c, RH):
        RH = float(np.clip(RH, 0.0, 1.0))
        pws = self.sat_vapor_pressure(T_c)
        pw = min(RH * pws, 0.99 * self.p)
        return max(0.0, 0.62198 * pw / max(self.p - pw, 1e-9))

    def w_to_RH(self, T_c, w):
        w = max(float(w), 0.0)
        pws = self.sat_vapor_pressure(T_c)
        pw = (w * self.p) / (0.62198 + w)
        return float(np.clip(pw / max(pws, 1e-9), 0.0, 1.0))

    def _normalize_series(self, arr, hours, default):
        if arr is None:
            return np.full(hours, default, dtype=float)
        a = np.asarray(arr, dtype=float)
        if len(a) == 0:
            return np.full(hours, default, dtype=float)
        if len(a) < hours:
            a = np.pad(a, (0, hours-len(a)), mode="edge")
        return a[:hours]

    def simulate(
        self, hours=8760, T_out_series=None, RH_out_series=None,
        airflow_series=None, wind_series=None,
        beef_mean_in=1.0, beef_mean_out=1.0,
        beef_E_kWh_range=(5.6, 10.0), beef_use_weight=False,
        beef_weight_mu=350.0, beef_weight_sigma=50.0,
        beef_EkWh_per_kg=7.8/350.0, beef_chill_hours=8,
        cop_sensible=3.0, cop_latent=3.0,
        rom=None, h_inner_min=5.0, h_inner_max=20.0,
        wall_L_m=0.2, wall_k_WmK=0.035,
        h_inner_beta=1.0, h_outer_base=5.7, h_outer_slope=3.8,
        A_inner=None, rom_theta=0.55,
        door_use_stochastic=False,
        door_open_frac_per_event=0.05, door_open_frac_std=0.02,
        door_open_frac_cap=0.25, door_ACH_during_open=20.0,
        door_ACH_std=5.0, door_ACH_cap=50.0,
        carcass_mass_avg_kg=350.0,
        cooling_capacity_kW=50.0,
        plot=False
    ):
        if hours <= 0:
            raise ValueError("hours must be > 0")

        t = np.arange(hours)
        if T_out_series is None:
            T_out_series = 10.0 + 10.0*np.sin(2*np.pi*(t-200)/8760.0)
        if RH_out_series is None:
            RH_out_series = np.clip(
                0.6 + 0.2*np.sin(2*np.pi*t/8760.0 + 1.0), 0.3, 0.95
            )
        T_out_series = self._normalize_series(T_out_series, hours, 10.0)
        RH_out_series = self._normalize_series(RH_out_series, hours, 0.6)
        airflow_series = self._normalize_series(airflow_series, hours, 5000.0)
        wind_series = self._normalize_series(wind_series, hours, 2.0)
        if A_inner is None:
            A_inner = self.A_inner_total

        T_in = np.empty(hours)
        RH_in = np.empty(hours)
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
        passive_floor_hits = np.zeros(hours, dtype=int)

        T_in[0] = self.T_set
        RH_in[0] = self.RH_set
        active_tasks = []
        ready_pool = 0
        dt = 3600.0
        AF_min, AF_max = self.airflow_bounds

        for h in range(hours):
            Tout = float(T_out_series[h])
            RHout = float(np.clip(RH_out_series[h], 0.0, 1.0))
            v = max(float(wind_series[h]), 0.0)

            n_in = int(np.random.poisson(max(beef_mean_in, 0.0)))
            n_out_demand = int(np.random.poisson(max(beef_mean_out, 0.0)))
            n_in_arr[h] = n_in

            events = n_in + n_out_demand
            door_events[h] = events

            AF_mech = float(np.clip(airflow_series[h], *self.airflow_bounds))
            if door_use_stochastic and events:
                AF_infil = 0.0
                f_sum = 0.0
                ach_sum = 0.0
                for _ in range(events):
                    f = min(
                        max(np.random.normal(
                            door_open_frac_per_event, door_open_frac_std
                        ), 0.0),
                        door_open_frac_cap
                    )
                    ach = min(
                        max(np.random.normal(
                            door_ACH_during_open, door_ACH_std
                        ), 0.0),
                        door_ACH_cap
                    )
                    AF_infil += f * ach * self.V
                    f_sum += f
                    ach_sum += ach
                door_frac_sum[h] = f_sum
                door_frac_mean[h] = f_sum/events
                door_ach_mean[h] = ach_sum/events
            else:
                AF_infil = (
                    events * door_open_frac_per_event *
                    door_ACH_during_open * self.V
                )
                door_frac_sum[h] = events * door_open_frac_per_event
                door_frac_mean[h] = door_open_frac_per_event if events else 0.0
                door_ach_mean[h] = door_ACH_during_open if events else 0.0

            AF_total = AF_mech + AF_infil
            airflow_mech[h] = AF_mech
            airflow_total[h] = AF_total
            infil_m3[h] = max(AF_total - AF_mech, 0.0)

            h_outer = max(h_outer_base + h_outer_slope*v, 0.1)
            u = 0.0 if AF_max <= AF_min else (AF_total-AF_min)/(AF_max-AF_min)
            u = float(np.clip(u, 0.0, 1.0))
            h_inner = h_inner_min + (h_inner_max-h_inner_min)*(u**h_inner_beta)
            h_inner = max(h_inner, 0.1)
            h_inner_series[h] = h_inner
            h_outer_series[h] = h_outer

            for _ in range(n_in):
                if beef_use_weight:
                    wkg = max(
                        50.0,
                        np.random.normal(beef_weight_mu, beef_weight_sigma)
                    )
                    E_car = wkg * beef_EkWh_per_kg
                    mass_in_kg[h] += wkg
                else:
                    E_car = np.random.uniform(*beef_E_kWh_range)
                    mass_in_kg[h] += carcass_mass_avg_kg
                active_tasks.append([float(E_car), int(max(beef_chill_hours, 1))])

            # Continuous cooling load. A carcass is not removed until the
            # complete assigned chilling duty is delivered.
            task_load = 0.0
            new_tasks = []
            for E_rem, hrs_left in active_tasks:
                share = E_rem / hrs_left
                task_load += share
                if hrs_left > 1:
                    new_tasks.append([E_rem-share, hrs_left-1])
                else:
                    ready_pool += 1
            active_tasks = new_tasks

            actual_out = min(n_out_demand, ready_pool)
            ready_pool -= actual_out
            n_out_arr[h] = actual_out
            mass_out_kg[h] = actual_out * (
                carcass_mass_avg_kg if not beef_use_weight else beef_weight_mu
            )
            beef_kWh[h] = task_load
            n_active_arr[h] = len(active_tasks)

            Tin = float(T_in[h-1] if h > 0 else T_in[0])
            RHin = float(RH_in[h-1] if h > 0 else RH_in[0])
            win = self.RH_to_w(Tin, RHin)
            wout = self.RH_to_w(Tout, RHout)
            m_dot = AF_total / 3600.0 * self.rho
            Q_beef_W = task_load * 1000.0

            # Wall conduction
            if rom is not None:
                rom.step_theta(
                    dt=dt, theta=max(rom_theta, 0.55),
                    Tin_K_old=Tin+273.15, Tout_K_old=Tout+273.15,
                    Tin_K_new=Tin+273.15, Tout_K_new=Tout+273.15,
                    h_inner_curr=h_inner, h_outer_curr=h_outer
                )
                T_surf_C = rom.surface_T() - 273.15
                U_wall = h_inner * A_inner
            else:
                # No ROM: use a physically consistent steady wall conductance.
                # Wall thermal resistance = 1/h_i + L/k + 1/h_o.
                # This keeps the non-ROM model physically comparable to ROM.
                L_wall = float(wall_L_m)
                k_wall = float(wall_k_WmK)
                U_per_area = 1.0 / (
                    1.0/max(h_inner, 1e-6)
                    + L_wall/max(k_wall, 1e-12)
                    + 1.0/max(h_outer, 1e-6)
                )
                U_wall = h_inner * A_inner
                T_surf_C = Tin + (
                    U_per_area * (Tout - Tin) /
                    max(h_inner, 1e-6)
                )

            # Exact solution of dT/dt = a*(T_eq-T) for the air node,
            # excluding active cooling.
            U_tot = U_wall + m_dot*self.cp
            Q_const = (
                U_wall*T_surf_C +
                m_dot*self.cp*Tout +
                Q_beef_W
            )
            a_air = U_tot / self.C_air
            b_air = Q_const / self.C_air

            if a_air > 1e-12:
                decay = math.exp(-a_air*dt)
                Tin_free = Tin*decay + (b_air/a_air)*(1.0-decay)
            else:
                Tin_free = Tin + b_air*dt

            # Thermostatic cooling. No cooling is applied below setpoint.
            # Cooling capacity is finite and expressed as heat removed
            # during the hour.
            T_floor, T_ceiling = self.T_bounds
            if Tin_free > self.T_set:
                E_need_J = (Tin_free - self.T_set) * self.C_air
                E_cap_J = max(cooling_capacity_kW, 0.0) * 3.6e6
                E_cool_J = min(E_need_J, E_cap_J)
                Q_sensible_kWh[h] = E_cool_J / 3.6e6
                Tin_next = Tin_free - E_cool_J/self.C_air
            else:
                Q_sensible_kWh[h] = 0.0
                Tin_next = Tin_free

            # Physical operating envelope. This only prevents numerical /
            # model extrapolation outside the declared room capability.
            if Tin_next < T_floor:
                passive_floor_hits[h] = 1
                Tin_next = T_floor
            elif Tin_next > T_ceiling:
                Tin_next = T_ceiling

            # Moisture transport.
            a_w = m_dot / (self.rho*self.V)
            if a_w > 1e-12:
                decay_w = math.exp(-a_w*dt)
                win_free = win*decay_w + wout*(1.0-decay_w)
            else:
                win_free = win

            w_set = self.RH_to_w(Tin_next, self.RH_set)
            if win_free > w_set:
                dm_remove = (win_free-w_set)*(self.rho*self.V)
                Q_latent_kWh[h] = dm_remove*self.h_fg/3.6e6
                w_next = w_set
            else:
                Q_latent_kWh[h] = 0.0
                w_next = max(win_free, 0.0)

            RH_next = self.w_to_RH(Tin_next, w_next)
            T_in[h] = Tin_next
            RH_in[h] = RH_next

            E_el_cool_kWh[h] = Q_sensible_kWh[h]/max(cop_sensible, 1e-6)
            E_el_latent_kWh[h] = Q_latent_kWh[h]/max(cop_latent, 1e-6)
            E_el_total_kWh[h] = (
                E_el_cool_kWh[h] + E_el_latent_kWh[h]
            )

        return dict(
            T_in=T_in, RH_in=RH_in,
            airflow_mech=airflow_mech, airflow_total=airflow_total,
            h_inner=h_inner_series, h_outer=h_outer_series,
            Q_sensible_kWh=Q_sensible_kWh,
            Q_latent_kWh=Q_latent_kWh,
            E_el_cool_kWh=E_el_cool_kWh,
            E_el_latent_kWh=E_el_latent_kWh,
            E_el_total_kWh=E_el_total_kWh,
            beef_kWh=beef_kWh,
            n_in=n_in_arr, n_out=n_out_arr, n_active=n_active_arr,
            door_events=door_events, door_frac_sum=door_frac_sum,
            door_frac_mean=door_frac_mean, door_ach_mean=door_ach_mean,
            infil_m3=infil_m3, mass_in_kg=mass_in_kg,
            mass_out_kg=mass_out_kg,
            passive_floor_hits=passive_floor_hits,
        )


# ============================================================
# KPI / OUTPUTS
# ============================================================

def compute_kpis(res, args, zone, tariff_eur_per_kWh=0.0,
                 grid_co2_kg_per_kWh=0.0, carcass_mass_avg_kg=350.0):
    T = res["T_in"]
    RH = res["RH_in"]
    hours = len(T)
    Qs, Ql = res["Q_sensible_kWh"], res["Q_latent_kWh"]
    Ec, El = res["E_el_cool_kWh"], res["E_el_latent_kWh"]
    Et = res["E_el_total_kWh"]
    n_in, n_out = res["n_in"], res["n_out"]
    mass_out = res.get("mass_out_kg", np.zeros_like(T))
    in_T = ((T >= zone.T_bounds[0]) & (T <= zone.T_bounds[1])).astype(int)
    in_RH = ((RH >= zone.RH_bounds[0]) & (RH <= zone.RH_bounds[1])).astype(int)

    throughput = int(min(n_in.sum(), n_out.sum()))
    mass_kg = float(mass_out.sum())
    if mass_kg <= 0:
        mass_kg = throughput*carcass_mass_avg_kg
    mass_t = mass_kg/1000.0
    total_el = float(Et.sum())
    kpi = dict(
        hours=hours,
        energy_sensible_kWh=float(Qs.sum()),
        energy_latent_kWh=float(Ql.sum()),
        energy_total_el_kWh=total_el,
        energy_el_sensible_kWh=float(Ec.sum()),
        energy_el_latent_kWh=float(El.sum()),
        beef_chilling_kWh=float(res["beef_kWh"].sum()),
        peak_power_kW=float(Et.max()),
        cop_eff_sensible=float(Qs.sum()/Ec.sum()) if Ec.sum() > 1e-12 else None,
        cop_eff_latent=float(Ql.sum()/El.sum()) if El.sum() > 1e-12 else None,
        carcasses_in=int(n_in.sum()),
        carcasses_out=int(n_out.sum()),
        carcasses_throughput=throughput,
        mass_throughput_tonnes=mass_t,
        kWh_el_per_carcass=(total_el/throughput if throughput else None),
        kWh_el_per_tonne=(total_el/mass_t if mass_t > 0 else None),
        infil_total_m3=float(res["infil_m3"].sum()),
        infil_total_m3_alt=float(
            np.maximum(res["airflow_total"]-res["airflow_mech"], 0).sum()
        ),
        T_violations=int((1-in_T).sum()),
        RH_violations=int((1-in_RH).sum()),
        T_in_bounds_pct=float(100*in_T.mean()),
        RH_in_bounds_pct=float(100*in_RH.mean()),
        T_min=float(T.min()), T_max=float(T.max()),
        RH_min=float(RH.min()), RH_max=float(RH.max()),
        T_bounds=list(zone.T_bounds), RH_bounds=list(zone.RH_bounds),
        h_inner_p5=pct(res["h_inner"], 5),
        h_inner_p50=pct(res["h_inner"], 50),
        h_inner_p95=pct(res["h_inner"], 95),
        h_outer_p5=pct(res["h_outer"], 5),
        h_outer_p50=pct(res["h_outer"], 50),
        h_outer_p95=pct(res["h_outer"], 95),
        door_total_events=int(res["door_events"].sum()),
        door_total_open_hours=float(res["door_frac_sum"].sum()),
        door_mean_open_minutes=(
            60*float(res["door_frac_sum"].sum())/
            max(int(res["door_events"].sum()), 1)
        ),
        door_mean_ach_when_open=float(
            np.mean(res["door_ach_mean"][res["door_events"] > 0])
            if np.any(res["door_events"] > 0) else 0.0
        ),
        passive_floor_hits=int(res["passive_floor_hits"].sum()),
        energy_intensity_kWh_el_per_m3_year=(
            total_el*(8760.0/hours)/zone.V
        ),
        tariff_eur_per_kWh=float(tariff_eur_per_kWh),
        grid_co2_kg_per_kWh=float(grid_co2_kg_per_kWh),
        energy_cost_eur=total_el*float(tariff_eur_per_kWh),
        energy_co2_kg=total_el*float(grid_co2_kg_per_kWh),
    )
    return kpi

def save_outputs(res, args, zone, kpi):
    os.makedirs(args.out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{args.tag}_{ts}"

    if getattr(args, "save_json", False):
        with open(
            os.path.join(args.out_dir, base+"_summary.json"),
            "w", encoding="utf-8"
        ) as f:
            json.dump(
                {"tag": args.tag, "timestamp": ts,
                 "hours": args.hours, "kpi": kpi},
                f, ensure_ascii=False, indent=2
            )

    if getattr(args, "save_csv", False):
        path = os.path.join(args.out_dir, base+"_timeseries.csv")
        fields = [
            "hour","T_in_C","RH_in","airflow_mech_m3ph","airflow_total_m3ph",
            "h_inner_Wm2K","h_outer_Wm2K",
            "Q_sensible_kWh","Q_latent_kWh",
            "E_el_cool_kWh","E_el_latent_kWh","E_el_total_kWh",
            "beef_kWh","n_in","n_out","n_active","door_events",
            "door_open_frac_sum_h","door_open_frac_mean_h",
            "door_ach_mean","infiltration_m3","mass_in_kg","mass_out_kg"
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(fields)
            for h in range(args.hours):
                w.writerow([
                    h, float(res["T_in"][h]), float(res["RH_in"][h]),
                    float(res["airflow_mech"][h]), float(res["airflow_total"][h]),
                    float(res["h_inner"][h]), float(res["h_outer"][h]),
                    float(res["Q_sensible_kWh"][h]), float(res["Q_latent_kWh"][h]),
                    float(res["E_el_cool_kWh"][h]),
                    float(res["E_el_latent_kWh"][h]),
                    float(res["E_el_total_kWh"][h]),
                    float(res["beef_kWh"][h]), int(res["n_in"][h]),
                    int(res["n_out"][h]), int(res["n_active"][h]),
                    int(res["door_events"][h]),
                    float(res["door_frac_sum"][h]),
                    float(res["door_frac_mean"][h]),
                    float(res["door_ach_mean"][h]),
                    float(res["infil_m3"][h]),
                    float(res["mass_in_kg"][h]),
                    float(res["mass_out_kg"][h])
                ])
        print("Saved CSV:", path)

    if getattr(args, "dump_config", False):
        with open(
            os.path.join(args.out_dir, base+"_config.json"),
            "w", encoding="utf-8"
        ) as f:
            json.dump(vars(args), f, ensure_ascii=False, indent=2)


# ============================================================
# ROM PREPARATION
# ============================================================

def prepare_rom_for_zone(args):
    d = parse_csv_floats(args.layers_thick_m)
    k = parse_csv_floats(args.layers_k_WmK)
    rho = parse_csv_floats(args.layers_rho_kgm3)
    cp = parse_csv_floats(args.layers_cp_Jkgm3) if hasattr(args, "layers_cp_Jkgm3") else parse_csv_floats(args.layers_cp_JkgK)

    Lx, keff, rhoeff, cpeff = effective_wall_from_layers(d, k, rho, cp)
    Nx = args.Nx

    # Build a richer snapshot library covering realistic room/ambient ranges.
    snapshots = []
    base_times = np.linspace(0.0, 48*3600.0, 480)
    T0 = np.full(Nx, 275.15)

    inner_cases = [-0.5, 2.0, 4.0]
    outer_cases = [0.0, 10.0, 25.0, 35.0]

    for TinC in inner_cases:
        for ToutC in outer_cases:
            def fL(t, TinC=TinC):
                return 273.15 + TinC
            def fR(t, ToutC=ToutC):
                return 273.15 + ToutC
            alpha, dx, Lop, SL, SR = build_1d_with_h(
                Nx, Lx, keff, rhoeff, cpeff,
                args.h_inner_base, args.h_outer_base
            )
            snaps = theta_integrate_1d(
                T0, base_times, alpha, Lop, SL, SR, fL, fR,
                theta=max(args.theta, 0.55)
            )
            snapshots.append(snaps)

    # One warm-outdoor seasonal transition.
    def fLstep(t):
        return 275.15
    def fRseason(t):
        return 273.15 + 10.0 + 10.0*math.sin(2*math.pi*t/(24*3600.0))

    alpha_snap, dx_snap, L_snap, SL_snap, SR_snap = build_1d_with_h(
        Nx, Lx, keff, rhoeff, cpeff,
        args.h_inner_base, args.h_outer_base
    )
    seasonal = theta_integrate_1d(
        T0, base_times, alpha_snap, L_snap,
        SL_snap, SR_snap, fLstep, fRseason,
        theta=max(args.theta, 0.55)
    )
    snapshots.append(seasonal)

    snapshot_matrix = np.concatenate(snapshots, axis=1)
    mu, Phi, _, captured = compute_pod(snapshot_matrix, args.r)
    alpha, dx, L0, LL, LR, SL, SR = build_1d_decomposed(
        Nx, Lx, keff, rhoeff, cpeff
    )
    parts = build_rom_decomposition(
        alpha, L0, LL, LR, SL, SR, Phi, mu
    )

    A_inner = 2*(
        args.room_L*args.room_W +
        args.room_L*args.room_H +
        args.room_W*args.room_H
    )
    rom = ConductionROM1D(
        Phi, mu, parts, dx, keff, T0_init_val=275.15
    )
    rom.pod_captured = captured
    rom.Lx = Lx
    rom.keff = keff
    return rom, A_inner


# ============================================================
# QC / LLM
# ============================================================

def scan_csv_stats(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            rows = list(r)
        if not rows:
            return None
        T = np.array([float(x["T_in_C"]) for x in rows])
        RH = np.array([float(x["RH_in"]) for x in rows])
        return dict(
            n_rows=len(rows), T_min=float(T.min()), T_max=float(T.max()),
            RH_min=float(RH.min()), RH_max=float(RH.max()),
            sum_in=int(sum(float(x["n_in"]) for x in rows)),
            sum_out=int(sum(float(x["n_out"]) for x in rows)),
            sum_door=int(sum(float(x["door_events"]) for x in rows)),
            sum_Eel=float(sum(float(x["E_el_total_kWh"]) for x in rows))
        )
    except Exception:
        return None

def load_scenarios(input_dir):
    out = []
    for path in glob.glob(os.path.join(input_dir, "*_summary.json")):
        base = os.path.basename(path).replace("_summary.json", "")
        cfg_path = os.path.join(input_dir, base+"_config.json")
        csv_path = os.path.join(input_dir, base+"_timeseries.csv")
        summ = read_json(path)
        out.append(dict(
            tag=summ.get("tag", base),
            summary=summ,
            kpi=summ.get("kpi", {}),
            config=read_json(cfg_path) if os.path.isfile(cfg_path) else {},
            csv_stats=scan_csv_stats(csv_path) if os.path.isfile(csv_path) else None,
            files=dict(summary=path,
                       config=cfg_path if os.path.isfile(cfg_path) else None,
                       csv=csv_path if os.path.isfile(csv_path) else None)
        ))
    return sorted(out, key=lambda x: x["tag"])

def qc_checks(one):
    k = one["kpi"]
    issues, warns = [], []
    for name in [
        "energy_total_el_kWh","energy_el_sensible_kWh","energy_el_latent_kWh",
        "energy_sensible_kWh","energy_latent_kWh"
    ]:
        v = k.get(name)
        if v is not None and v < -1e-9:
            issues.append(f"Negative KPI {name}={v}")

    if k.get("T_min", 0) < k["T_bounds"][0] - 1e-9:
        warns.append(f"T below lower bound: {k['T_min']}")
    if k.get("T_max", 0) > k["T_bounds"][1] + 1e-9:
        warns.append(f"T above upper bound: {k['T_max']}")
    if k.get("RH_min", 0) < 0 or k.get("RH_max", 1) > 1:
        issues.append("RH outside [0,1].")

    Et = k.get("energy_total_el_kWh", 0)
    Ec = k.get("energy_el_sensible_kWh", 0)
    El = k.get("energy_el_latent_kWh", 0)
    if abs(Et-(Ec+El)) > max(1e-2, 0.01*max(Et,1.0)):
        warns.append("Etot != Ec+El.")

    return dict(
        status="red" if issues else ("amber" if warns else "green"),
        issues=issues, warns=warns
    )

def build_prompt(scenarios, qc):
    lines = [
        "You are a skeptical energy engineer.",
        "Check units, physics, balances, T/RH bounds, event consistency and energy intensity."
    ]
    for s in scenarios:
        k=s["kpi"]
        lines.append(f"\n# {s['tag']}")
        for key in [
            "hours","energy_total_el_kWh","energy_cost_eur",
            "energy_co2_kg","energy_sensible_kWh","energy_latent_kWh",
            "beef_chilling_kWh","carcasses_throughput","T_min","T_max",
            "RH_min","RH_max","T_violations","RH_violations",
            "passive_floor_hits","kWh_el_per_tonne"
        ]:
            if key in k:
                lines.append(f"- {key}: {k[key]}")
        q=qc[s["tag"]]
        lines.append(f"- QC: {q['status']}, issues={len(q['issues'])}, warnings={len(q['warns'])}")
    return "\n".join(lines)

def call_gemini(model, api_key, prompt):
    response = None
    url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents":[{"parts":[{"text":prompt}]}],
        "systemInstruction":{"parts":[{"text":"You are a precise, skeptical energy engineer."}]},
        "generationConfig":{"temperature":0.2,"maxOutputTokens":2200}
    }
    try:
        response = requests.post(
            url, headers={"Content-Type":"application/json"},
            json=payload, timeout=60
        )
        response.raise_for_status()
        data=response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except requests.exceptions.RequestException as e:
        extra = f" | Server: {response.text}" if response is not None else ""
        return f"_Gemini API error: {e}{extra}_"

def write_report(path, scenarios, qc_map, llm_text=None):
    with open(path,"w",encoding="utf-8") as f:
        f.write(f"# Cold Storage QC\n\nGenerated: {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n")
        for tag,q in qc_map.items():
            f.write(f"## {tag}: {q['status']}\n")
            for x in q["issues"]:
                f.write(f"- ISSUE: {x}\n")
            for x in q["warns"]:
                f.write(f"- WARNING: {x}\n")
            if not q["issues"] and not q["warns"]:
                f.write("- No QC issues.\n")
        if llm_text:
            f.write("\n## LLM Analysis\n\n"+llm_text+"\n")
    return path


# ============================================================
# EXECUTORS
# ============================================================

def make_default_args(**overrides):
    base=dict(
        mode="zone", layers_thick_m="0.2", layers_k_WmK="0.035",
        layers_rho_kgm3="30.0", layers_cp_JkgK="1400.0",
        Nx=81, r=6, h_inner_base=10.0, h_outer_base=13.3,
        h_outer_slope=3.8, theta=0.55, room_L=10.0,
        room_W=10.0, room_H=4.0, hours=168, rom_enable=False,
        beef_mean_in=1.0, beef_mean_out=1.0, beef_E_min=5.6,
        beef_E_max=10.0, beef_use_weight=False, beef_weight_mu=350.0,
        beef_weight_sigma=50.0, beef_EkWh_per_kg=7.8/350.0,
        beef_chill_hours=8, carcass_mass_avg_kg=350.0,
        cop_sensible=3.0, cop_latent=3.0, wind_series="",
        h_inner_min=5.0, h_inner_max=20.0, h_inner_beta=1.0,
        door_use_stochastic=False, door_open_frac_per_event=0.05,
        door_open_frac_std=0.02, door_open_frac_cap=0.25,
        door_ACH_during_open=20.0, door_ACH_std=5.0,
        door_ACH_cap=50.0, tariff_eur_per_kWh=0.0,
        grid_co2_kg_per_kWh=0.0, cooling_capacity_kW=50.0,
        wall_L_m=0.2, wall_k_WmK=0.035,
        tag="baseline", out_dir=OUT_DIR, save_csv=True, save_json=True,
        dump_config=True, Tinit=283.15, Tinf_in=263.15,
        Tinf_out=293.15, t_step=300.0, Tend=3600.0, Nt=400,
        train_steps=1500, device="cpu", batch_file=""
    )
    base.update(overrides)
    return SimpleNamespace(**base)

def run_zone(args):
    rom = None
    A_inner = None
    if args.rom_enable:
        rom,A_inner=prepare_rom_for_zone(args)

    wind=parse_csv_floats(args.wind_series)
    zone=ColdRoomZone(
        room_L=args.room_L, room_W=args.room_W, room_H=args.room_H,
        T_set=2.0, RH_set=0.80,
        T_bounds=(-0.5,4.0), RH_bounds=(0.75,0.90)
    )
    res=zone.simulate(
        hours=args.hours, wind_series=wind,
        beef_mean_in=args.beef_mean_in, beef_mean_out=args.beef_mean_out,
        beef_E_kWh_range=(args.beef_E_min,args.beef_E_max),
        beef_use_weight=args.beef_use_weight,
        beef_weight_mu=args.beef_weight_mu,
        beef_weight_sigma=args.beef_weight_sigma,
        beef_EkWh_per_kg=args.beef_EkWh_per_kg,
        beef_chill_hours=args.beef_chill_hours,
        cop_sensible=args.cop_sensible, cop_latent=args.cop_latent,
        rom=rom, h_inner_min=args.h_inner_min,
        h_inner_max=args.h_inner_max, h_inner_beta=args.h_inner_beta,
        h_outer_base=args.h_outer_base, h_outer_slope=args.h_outer_slope,
        A_inner=A_inner, rom_theta=args.theta,
        wall_L_m=args.wall_L_m, wall_k_WmK=args.wall_k_WmK,
        door_use_stochastic=args.door_use_stochastic,
        door_open_frac_per_event=args.door_open_frac_per_event,
        door_open_frac_std=args.door_open_frac_std,
        door_open_frac_cap=args.door_open_frac_cap,
        door_ACH_during_open=args.door_ACH_during_open,
        door_ACH_std=args.door_ACH_std, door_ACH_cap=args.door_ACH_cap,
        carcass_mass_avg_kg=args.carcass_mass_avg_kg,
        cooling_capacity_kW=args.cooling_capacity_kW
    )
    kpi=compute_kpis(
        res,args,zone,args.tariff_eur_per_kWh,
        args.grid_co2_kg_per_kWh,args.carcass_mass_avg_kg
    )
    print(f"Summary for {args.hours} h:")
    print(f"  Sensible cooling: {kpi['energy_sensible_kWh']:.2f} kWh_th")
    print(f"  Latent cooling:   {kpi['energy_latent_kWh']:.2f} kWh_th")
    print(f"  Electric energy:  {kpi['energy_total_el_kWh']:.2f} kWh_el")
    print(f"  Beef chilling:    {kpi['beef_chilling_kWh']:.2f} kWh_th")
    print(f"  T range:          {kpi['T_min']:.3f} .. {kpi['T_max']:.3f} C")
    print(f"  RH range:         {kpi['RH_min']:.4f} .. {kpi['RH_max']:.4f}")
    print(f"  Floor hits:       {kpi['passive_floor_hits']}")
    print(f"  In/Out:            {kpi['carcasses_in']} / {kpi['carcasses_out']}")
    save_outputs(res,args,zone,kpi)
    return res,kpi

def run_conduction(args):
    d=parse_csv_floats(args.layers_thick_m)
    k=parse_csv_floats(args.layers_k_WmK)
    rho=parse_csv_floats(args.layers_rho_kgm3)
    cp=parse_csv_floats(args.layers_cp_JkgK)
    Lx,keff,rhoeff,cpeff=effective_wall_from_layers(d,k,rho,cp)
    alpha,dx,L,SL,SR=build_1d_with_h(
        args.Nx,Lx,keff,rhoeff,cpeff,
        args.h_inner_base,args.h_outer_base
    )
    times=np.linspace(0,args.Tend,args.Nt)
    T0=np.full(args.Nx,args.Tinit)
    def fL(t):
        return args.Tinit if t<args.t_step else args.Tinf_in
    def fR(t):
        return args.Tinf_out
    snaps=theta_integrate_1d(
        T0,times,alpha,L,SL,SR,fL,fR,args.theta
    )
    mu,Phi,_,captured=compute_pod(snaps,args.r)
    _, _, L0, LL, LR, SL2, SR2 = build_1d_decomposed(
        args.Nx, Lx, keff, rhoeff, cpeff
    )
    parts=build_rom_decomposition(
        alpha, L0, LL, LR, SL2, SR2, Phi, mu
    )
    a0=Phi.T@(T0-mu)
    A_rom=Phi.T@(alpha*L)@Phi
    B_list=[
        Phi.T@(alpha*SL),
        Phi.T@(alpha*SR)
    ]
    c_rom=Phi.T@(alpha*(L@mu))
    def TL(tt):
        x=tt.squeeze(-1)
        return torch.where(
            x<args.t_step,
            torch.full_like(x,args.Tinit),
            torch.full_like(x,args.Tinf_in)
        )
    def TR(tt):
        return torch.full_like(tt.squeeze(-1),args.Tinf_out)
    model=train_pinn_for_rom(
        A_rom,B_list,c_rom,a0,args.Tend,
        device=args.device,steps=args.train_steps,
        t_step=args.t_step,
        Tinf_time_fns=[TL,TR],verbose=True,
        target_times=times, target_a=(Phi.T @ (snaps-mu[:,None])).T,
        data_weight=20.0
    )
    with torch.no_grad():
        tg=to_tensor(times.reshape(-1,1),args.device)
        ap=model(tg).cpu().numpy().T
    Tpred=mu[:,None]+Phi@ap
    err=np.max(np.abs(Tpred-snaps))
    final_rmse=float(np.sqrt(np.mean((Tpred[:,-1]-snaps[:,-1])**2)))
    print(f"POD captured energy: {captured*100:.5f}%")
    print(f"PINN/FDM max error: {err:.6f} K")
    print(f"PINN/FDM final RMSE: {final_rmse:.6f} K")
    out=os.path.join(args.out_dir,"conduction_validation.png")
    plt.figure(figsize=(8,4))
    x=np.linspace(0,Lx,args.Nx)
    plt.plot(x,snaps[:,-1],label="FDM final")
    plt.plot(x,Tpred[:,-1],"--",label="PINN-ROM final")
    plt.xlabel("x (m)"); plt.ylabel("T (K)")
    plt.legend(); plt.tight_layout(); plt.savefig(out,dpi=150); plt.close()
    return dict(max_error=err, final_rmse=final_rmse,
                captured_energy=captured, plot=out)

def run_batch_cli(args):
    if not os.path.isfile(args.batch_file):
        raise FileNotFoundError(args.batch_file)
    try:
        import yaml
        with open(args.batch_file,encoding="utf-8") as f:
            data=yaml.safe_load(f)
    except Exception:
        with open(args.batch_file,encoding="utf-8") as f:
            data=json.load(f)
    scenarios=data if isinstance(data,list) else data.get("scenarios",[])
    for sc in scenarios:
        local=argparse.Namespace(**vars(args))
        for k,v in sc.items():
            setattr(local,k,v)
        local.save_json=True; local.save_csv=True; local.dump_config=True
        run_zone(local)

def run_llm_cli(args):
    scenarios=load_scenarios(args.input_dir)
    if not scenarios:
        print("No summaries found:",args.input_dir); return
    qc={s["tag"]:qc_checks(s) for s in scenarios}
    llm=None
    if args.use_llm:
        if not args.api_key:
            raise RuntimeError("No Gemini API key supplied.")
        llm=call_gemini(args.model,args.api_key,build_prompt(scenarios,qc))
    path=os.path.join(args.input_dir,args.report)
    write_report(path,scenarios,qc,llm)
    for tag,q in qc.items():
        print(f"{tag}: {q['status']} issues={len(q['issues'])} warnings={len(q['warns'])}")
    print("Report:",path)


# ============================================================
# GRADIO
# ============================================================

def run_single_scenario(
    tag,hours,rom_enable,wind_series,door_use_stochastic,
    beef_mean_in,beef_mean_out,cop_sensible,cop_latent,
    tariff,grid_co2,h_inner_min,h_inner_max,h_inner_beta,
    h_outer_base,h_outer_slope
):
    args=make_default_args(
        tag=str(tag or "ui_demo"),hours=int(hours),
        rom_enable=bool(rom_enable),wind_series=str(wind_series or ""),
        door_use_stochastic=bool(door_use_stochastic),
        beef_mean_in=float(beef_mean_in),beef_mean_out=float(beef_mean_out),
        cop_sensible=float(cop_sensible),cop_latent=float(cop_latent),
        tariff_eur_per_kWh=float(tariff),
        grid_co2_kg_per_kWh=float(grid_co2),
        h_inner_min=float(h_inner_min),h_inner_max=float(h_inner_max),
        h_inner_beta=float(h_inner_beta),
        h_outer_base=float(h_outer_base),
        h_outer_slope=float(h_outer_slope)
    )
    _,kpi=run_zone(args)
    return json.dumps(kpi,ensure_ascii=False,indent=2), []

def run_batch_ui(yaml_file):
    if yaml_file is None:
        return "Upload a batch file.", []
    dst=os.path.join(OUT_DIR,"scenarios_uploaded.yaml")
    shutil.copyfile(yaml_file,dst)
    args=make_default_args(
        hours=168,rom_enable=True,beef_mean_in=2.0,beef_mean_out=2.0,
        wind_series="3",door_use_stochastic=True,
        h_inner_beta=1.0,tariff_eur_per_kWh=0.2,
        grid_co2_kg_per_kWh=0.35,tag="batch",batch_file=dst
    )
    run_batch_cli(args)
    files=sorted(
        glob.glob(os.path.join(OUT_DIR,"*_summary.json")),
        key=os.path.getmtime,reverse=True
    )[:30]
    return f"Batch finished. Produced {len(files)} summaries.", files

def run_llm_ui(input_dir,use_llm,model,api_key):
    if not os.path.isdir(input_dir):
        return "Input dir not found", "", []
    scenarios=load_scenarios(input_dir)
    if not scenarios:
        return "No summaries found", "", []
    qc={s["tag"]:qc_checks(s) for s in scenarios}
    text=None
    if use_llm:
        key=api_key or os.getenv("GEMINI_API_KEY","")
        if not key:
            return "No Gemini API key", "", []
        text=call_gemini(model,key,build_prompt(scenarios,qc))
    path=os.path.join(input_dir,"analysis_report.md")
    write_report(path,scenarios,qc,text)
    with open(path,encoding="utf-8") as f:
        md=f.read()
    return "Analysis complete.",md,[path]

def launch_ui():
    with gr.Blocks() as demo:
        gr.Markdown("# Cold Storage Thermal Model")
        with gr.Tab("Single Scenario"):
            with gr.Row():
                tag=gr.Textbox(label="Scenario tag",value="ui_demo")
                hours=gr.Slider(label="Hours",minimum=24,maximum=8760,step=24,value=168)
                rom=gr.Checkbox(label="Enable ROM",value=True)
                door=gr.Checkbox(label="Stochastic doors",value=True)
            wind=gr.Textbox(label="Wind series",value="3")
            with gr.Row():
                cop_s=gr.Number(label="COP sensible",value=3.0)
                cop_l=gr.Number(label="COP latent",value=3.0)
                tariff=gr.Number(label="Tariff €/kWh",value=0.2)
                co2=gr.Number(label="kgCO2/kWh",value=0.35)
            with gr.Row():
                bi=gr.Number(label="Arrivals/h",value=2.0)
                bo=gr.Number(label="Departures/h",value=2.0)
            with gr.Row():
                hmin=gr.Number(label="h inner min",value=6.0)
                hmax=gr.Number(label="h inner max",value=20.0)
                beta=gr.Number(label="h inner beta",value=1.2)
            with gr.Row():
                hob=gr.Number(label="h outer base",value=5.7)
                hos=gr.Number(label="h outer slope",value=3.8)
            btn=gr.Button("Run scenario")
            out=gr.Code(label="KPI JSON")
            btn.click(
                run_single_scenario,
                [tag,hours,rom,wind,door,bi,bo,cop_s,cop_l,tariff,co2,
                 hmin,hmax,beta,hob,hos],[out]
            )

        with gr.Tab("Batch"):
            f=gr.File(label="YAML/JSON",file_count="single")
            b=gr.Button("Run batch")
            log=gr.Textbox()
            bf=gr.Files()
            b.click(run_batch_ui,[f],[log,bf])

        with gr.Tab("LLM Analysis"):
            idir=gr.Textbox(value=OUT_DIR,label="Input dir")
            use=gr.Checkbox(value=False,label="Use Gemini")
            model=gr.Textbox(value="gemini-2.5-flash",label="Gemini model")
            key=gr.Textbox(type="password",label="API key")
            lb=gr.Button("Run analysis")
            status=gr.Textbox(label="Status")
            md=gr.Markdown()
            rf=gr.Files()
            lb.click(run_llm_ui,[idir,use,model,key],[status,md,rf])
    demo.queue().launch()

# ============================================================
# MAIN
# ============================================================

def main():
    if len(sys.argv)==1:
        launch_ui()
        return

    p=argparse.ArgumentParser()
    p.add_argument("--mode",default="zone",choices=["zone","conduction"])
    p.add_argument("--layers_thick_m",default="0.2")
    p.add_argument("--layers_k_WmK",default="0.035")
    p.add_argument("--layers_rho_kgm3",default="30.0")
    p.add_argument("--layers_cp_JkgK",default="1400.0")
    p.add_argument("--Nx",type=int,default=81)
    p.add_argument("--r",type=int,default=6)
    p.add_argument("--h_inner_base",type=float,default=10.0)
    p.add_argument("--h_outer_base",type=float,default=13.3)
    p.add_argument("--h_outer_slope",type=float,default=3.8)
    p.add_argument("--Tinit",type=float,default=283.15)
    p.add_argument("--Tinf_in",type=float,default=263.15)
    p.add_argument("--Tinf_out",type=float,default=293.15)
    p.add_argument("--t_step",type=float,default=300.0)
    p.add_argument("--Tend",type=float,default=3600.0)
    p.add_argument("--Nt",type=int,default=400)
    p.add_argument("--train_steps",type=int,default=1500)
    p.add_argument("--device",default="cpu")
    p.add_argument("--theta",type=float,default=0.55)
    p.add_argument("--room_L",type=float,default=10.0)
    p.add_argument("--room_W",type=float,default=10.0)
    p.add_argument("--room_H",type=float,default=4.0)
    p.add_argument("--hours",type=int,default=168)
    p.add_argument("--rom_enable",action="store_true")
    p.add_argument("--beef_mean_in",type=float,default=1.0)
    p.add_argument("--beef_mean_out",type=float,default=1.0)
    p.add_argument("--beef_E_min",type=float,default=5.6)
    p.add_argument("--beef_E_max",type=float,default=10.0)
    p.add_argument("--beef_use_weight",action="store_true")
    p.add_argument("--beef_weight_mu",type=float,default=350.0)
    p.add_argument("--beef_weight_sigma",type=float,default=50.0)
    p.add_argument("--beef_EkWh_per_kg",type=float,default=7.8/350.0)
    p.add_argument("--beef_chill_hours",type=int,default=8)
    p.add_argument("--carcass_mass_avg_kg",type=float,default=350.0)
    p.add_argument("--cop_sensible",type=float,default=3.0)
    p.add_argument("--cop_latent",type=float,default=3.0)
    p.add_argument("--wall_L_m",type=float,default=0.2)
    p.add_argument("--wall_k_WmK",type=float,default=0.035)
    p.add_argument("--wind_series",default="")
    p.add_argument("--h_inner_min",type=float,default=5.0)
    p.add_argument("--h_inner_max",type=float,default=20.0)
    p.add_argument("--h_inner_beta",type=float,default=1.0)
    p.add_argument("--door_use_stochastic",action="store_true")
    p.add_argument("--door_open_frac_per_event",type=float,default=0.05)
    p.add_argument("--door_open_frac_std",type=float,default=0.02)
    p.add_argument("--door_open_frac_cap",type=float,default=0.25)
    p.add_argument("--door_ACH_during_open",type=float,default=20.0)
    p.add_argument("--door_ACH_std",type=float,default=5.0)
    p.add_argument("--door_ACH_cap",type=float,default=50.0)
    p.add_argument("--cooling_capacity_kW",type=float,default=50.0)
    p.add_argument("--tariff_eur_per_kWh",type=float,default=0.0)
    p.add_argument("--grid_co2_kg_per_kWh",type=float,default=0.0)
    p.add_argument("--tag",default="baseline")
    p.add_argument("--out_dir",default=OUT_DIR)
    p.add_argument("--save_csv",action="store_true")
    p.add_argument("--save_json",action="store_true")
    p.add_argument("--dump_config",action="store_true")
    p.add_argument("--batch_file",default="")
    p.add_argument("--input_dir",default="")
    p.add_argument("--use_llm",action="store_true")
    p.add_argument("--model",default="gemini-2.5-flash")
    p.add_argument("--api_key",default=os.getenv("GEMINI_API_KEY",""))
    p.add_argument("--report",default="analysis_report.md")
    args=p.parse_args()

    if args.input_dir:
        run_llm_cli(args)
    elif args.batch_file:
        run_batch_cli(args)
    elif args.mode=="conduction":
        run_conduction(args)
    else:
        run_zone(args)

if __name__=="__main__":
    main()
