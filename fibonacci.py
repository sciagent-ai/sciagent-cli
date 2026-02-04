#!/usr/bin/env python3
"""
Fibonacci Number Calculator

This script provides multiple methods to calculate Fibonacci numbers:
1. Recursive approach (simple but inefficient for large numbers)
2. Iterative approach (efficient)
3. Memoized recursive approach (efficient with caching)
4. Generator approach (memory efficient for sequences)
"""

import time
from functools import lru_cache


def fibonacci_recursive(n):
    """
    Calculate nth Fibonacci number using recursion.
    
    Args:
        n (int): Position in Fibonacci sequence (0-indexed)
        
    Returns:
        int: The nth Fibonacci number
        
    Note: This is inefficient for large n due to repeated calculations.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n <= 1:
        return n
    return fibonacci_recursive(n - 1) + fibonacci_recursive(n - 2)


def fibonacci_iterative(n):
    """
    Calculate nth Fibonacci number using iteration.
    
    Args:
        n (int): Position in Fibonacci sequence (0-indexed)
        
    Returns:
        int: The nth Fibonacci number
        
    This is efficient with O(n) time and O(1) space complexity.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n <= 1:
        return n
    
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


@lru_cache(maxsize=None)
def fibonacci_memoized(n):
    """
    Calculate nth Fibonacci number using memoized recursion.
    
    Args:
        n (int): Position in Fibonacci sequence (0-indexed)
        
    Returns:
        int: The nth Fibonacci number
        
    Uses LRU cache to store previously calculated values.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n <= 1:
        return n
    return fibonacci_memoized(n - 1) + fibonacci_memoized(n - 2)


def fibonacci_generator(max_n):
    """
    Generate Fibonacci sequence up to the max_n-th number.
    
    Args:
        max_n (int): Maximum position in sequence to generate
        
    Yields:
        int: Next Fibonacci number in sequence
        
    Memory efficient for generating sequences.
    """
    if max_n < 0:
        return
    
    a, b = 0, 1
    for i in range(max_n + 1):
        if i == 0:
            yield a
        elif i == 1:
            yield b
        else:
            a, b = b, a + b
            yield b


def fibonacci_sequence(count):
    """
    Generate a list of the first 'count' Fibonacci numbers.
    
    Args:
        count (int): Number of Fibonacci numbers to generate
        
    Returns:
        list: List of Fibonacci numbers
    """
    if count <= 0:
        return []
    
    sequence = []
    a, b = 0, 1
    
    for i in range(count):
        if i == 0:
            sequence.append(a)
        elif i == 1:
            sequence.append(b)
        else:
            a, b = b, a + b
            sequence.append(b)
    
    return sequence


def benchmark_methods(n):
    """
    Benchmark different Fibonacci calculation methods.
    
    Args:
        n (int): Position in Fibonacci sequence to calculate
    """
    print(f"\nBenchmarking Fibonacci calculation for n={n}")
    print("-" * 50)
    
    # Iterative method
    start_time = time.time()
    result_iterative = fibonacci_iterative(n)
    time_iterative = time.time() - start_time
    print(f"Iterative:  {result_iterative} (Time: {time_iterative:.6f}s)")
    
    # Memoized method
    fibonacci_memoized.cache_clear()  # Clear cache for fair comparison
    start_time = time.time()
    result_memoized = fibonacci_memoized(n)
    time_memoized = time.time() - start_time
    print(f"Memoized:   {result_memoized} (Time: {time_memoized:.6f}s)")
    
    # Recursive method (only for small n to avoid long wait times)
    if n <= 35:
        start_time = time.time()
        result_recursive = fibonacci_recursive(n)
        time_recursive = time.time() - start_time
        print(f"Recursive:  {result_recursive} (Time: {time_recursive:.6f}s)")
    else:
        print(f"Recursive:  Skipped (too slow for n={n})")


def main():
    """Main function demonstrating various Fibonacci calculations."""
    print("Fibonacci Number Calculator")
    print("=" * 40)
    
    # Calculate specific Fibonacci numbers
    test_numbers = [0, 1, 5, 10, 20, 30]
    
    print("\nFibonacci numbers at specific positions:")
    for n in test_numbers:
        fib_n = fibonacci_iterative(n)
        print(f"F({n}) = {fib_n}")
    
    # Generate Fibonacci sequence
    print(f"\nFirst 15 Fibonacci numbers:")
    sequence = fibonacci_sequence(15)
    print(sequence)
    
    # Using generator
    print(f"\nFirst 10 Fibonacci numbers using generator:")
    fib_gen = list(fibonacci_generator(9))
    print(fib_gen)
    
    # Benchmark different methods
    benchmark_methods(30)
    
    # Interactive mode
    print("\n" + "=" * 40)
    print("Interactive Mode")
    print("Enter a number to calculate its Fibonacci value (or 'quit' to exit)")
    
    while True:
        try:
            user_input = input("\nEnter n: ").strip()
            if user_input.lower() in ['quit', 'exit', 'q']:
                break
            
            n = int(user_input)
            if n < 0:
                print("Please enter a non-negative integer.")
                continue
            
            result = fibonacci_iterative(n)
            print(f"F({n}) = {result}")
            
            # Show sequence if n is small
            if n <= 20:
                sequence = fibonacci_sequence(n + 1)
                print(f"Sequence: {sequence}")
                
        except ValueError:
            print("Please enter a valid integer.")
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break


if __name__ == "__main__":
    main()