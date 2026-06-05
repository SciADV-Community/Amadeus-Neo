# Paste your AMADEUS ASCII art between the triple quotes.
# Leave it blank if you do not want ASCII art shown in embeds.
AMADEUS_ART = r"""

                                                                                      dddddddd                                                       
               AAA                                                                    d::::::d                                                       
              A:::A                                                                   d::::::d                                                       
             A:::::A                                                                  d::::::d                                                       
            A:::::::A                                                                 d:::::d                                                        
           A:::::::::A              mmmmmmm    mmmmmmm     aaaaaaaaaaaaa      ddddddddd:::::d     eeeeeeeeeeee    uuuuuu    uuuuuu      ssssssssss   
          A:::::A:::::A           mm:::::::m  m:::::::mm   a::::::::::::a   dd::::::::::::::d   ee::::::::::::ee  u::::u    u::::u    ss::::::::::s  
         A:::::A A:::::A         m::::::::::mm::::::::::m  aaaaaaaaa:::::a d::::::::::::::::d  e::::::eeeee:::::eeu::::u    u::::u  ss:::::::::::::s 
        A:::::A   A:::::A        m::::::::::::::::::::::m           a::::ad:::::::ddddd:::::d e::::::e     e:::::eu::::u    u::::u  s::::::ssss:::::s
       A:::::A     A:::::A       m:::::mmm::::::mmm:::::m    aaaaaaa:::::ad::::::d    d:::::d e:::::::eeeee::::::eu::::u    u::::u   s:::::s  ssssss 
      A:::::AAAAAAAAA:::::A      m::::m   m::::m   m::::m  aa::::::::::::ad:::::d     d:::::d e:::::::::::::::::e u::::u    u::::u     s::::::s      
     A:::::::::::::::::::::A     m::::m   m::::m   m::::m a::::aaaa::::::ad:::::d     d:::::d e::::::eeeeeeeeeee  u::::u    u::::u        s::::::s   
    A:::::AAAAAAAAAAAAA:::::A    m::::m   m::::m   m::::ma::::a    a:::::ad:::::d     d:::::d e:::::::e           u:::::uuuu:::::u  ssssss   s:::::s 
   A:::::A             A:::::A   m::::m   m::::m   m::::ma::::a    a:::::ad::::::ddddd::::::dde::::::::e          u:::::::::::::::uus:::::ssss::::::s
  A:::::A               A:::::A  m::::m   m::::m   m::::ma:::::aaaa::::::a d:::::::::::::::::d e::::::::eeeeeeee   u:::::::::::::::us::::::::::::::s 
 A:::::A                 A:::::A m::::m   m::::m   m::::m a::::::::::aa:::a d:::::::::ddd::::d  ee:::::::::::::e    uu::::::::uu:::u s:::::::::::ss  
AAAAAAA                   AAAAAAAmmmmmm   mmmmmm   mmmmmm  aaaaaaaaaa  aaaa  ddddddddd   ddddd    eeeeeeeeeeeeee      uuuuuuuu  uuuu  sssssssssss    
"""

TAGLINE_ART = r"""
        A M A D E U S  //  ＣＯＲＥ ＯＮＬＩＮＥ"""

DIVIDER = r"""

＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼＼
／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／／"""

def build_art_block() -> str:
    """
    Returns the AMADEUS ASCII art.

    If AMADEUS_ART is blank, this returns an empty string.
    """

    if not AMADEUS_ART or not TAGLINE_ART or not DIVIDER:
        return ""

    return DIVIDER + AMADEUS_ART + TAGLINE_ART + DIVIDER

def build_divider() -> str:
    """
    Returns the AMADEUS stylized divider

    If DIVIDER is blank, this returns an empty string.
    """

    if not DIVIDER:
        return ""

    return DIVIDER