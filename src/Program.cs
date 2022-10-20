namespace TerminalStress
{
    using System;
    using System.Diagnostics;
    using System.IO;
    using System.Text;

    class Program
    {
        static void Main(string[] args)
        {

            Random r = new Random();

#pragma warning disable SYSLIB0001
            Console.OutputEncoding = args.Length > 0 ? Encoding.UTF7 : Encoding.UTF8;
            string s = string.Empty;

            while (true)
            {
                try
                {
                    Console.SetCursorPosition(r.Next(Console.WindowWidth), r.Next(Console.WindowHeight));
                }
                catch
                {
                    Console.Write("☠️");
                }

                char c = (char)r.Next(0, 0xFFFF);
                Console.Write(c);
                s += c;

                if (r.Next(1_000) == 1)
                {
                    Console.Clear();
                    Console.WriteLine(s);
                }

                if (s.Length > 1_000)
                {
                    s = string.Empty;
                }

                if (r.Next(1_000_000) < 100)
                {
                    for (int i = 0; i < 100; i++)
                    {
                        Console.Write("☠️");
                    }
                }
            }
        }
    }
}