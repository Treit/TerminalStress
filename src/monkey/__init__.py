"""
Windows Terminal Monkey Stress Tester

Randomized stress testing for Windows Terminal to find hangs, crashes, and memory leaks.
Simulates chaotic user behavior: splitting/resizing/closing panes, resizing windows,
sending random keyboard input, and more.

Usage:
    python -m monkey.runner [--duration SECONDS] [--seed SEED]
"""
